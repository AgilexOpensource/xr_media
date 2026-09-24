"""Bidirectional WebRTC audio module."""

import asyncio
import inspect
import logging
from collections import deque
from dataclasses import dataclass
from fractions import Fraction
import threading
import time
from typing import Any, Callable, Dict, List, Set

from aiortc import RTCPeerConnection
from aiortc.mediastreams import MediaStreamTrack
from av import AudioFrame as AvAudioFrame, AudioResampler
import numpy as np

from .frame import AudioFrame
from .media import MediaModule, PeerMediaContext, match_browser_input

log = logging.getLogger("xr_media.media.audio")


class _AudioBroadcastBuffer:
    def __init__(self, capacity: int = 200) -> None:
        self._frames = deque(maxlen=capacity)
        self._sequence = 0
        self._lock = threading.Lock()

    def push(self, frame: AudioFrame) -> None:
        with self._lock:
            self._sequence += 1
            self._frames.append((self._sequence, frame))

    def current_sequence(self) -> int:
        with self._lock:
            return self._sequence

    def next_after(self, sequence: int):
        with self._lock:
            for item_sequence, frame in self._frames:
                if item_sequence > sequence:
                    return item_sequence, frame
        return sequence, None


class _AudioOutputTrack(MediaStreamTrack):
    kind = "audio"

    def __init__(self, module: "AudioModule", buffer: _AudioBroadcastBuffer) -> None:
        super().__init__()
        self._module = module
        self._buffer = buffer
        self._sequence = buffer.current_sequence()
        self._pts = 0
        self._next_frame_at = None
        self._active = asyncio.Event()
        self._active.set()

    def pause(self) -> None:
        self._active.clear()

    def resume(self) -> None:
        self._active.set()

    async def recv(self) -> AvAudioFrame:
        await self._active.wait()
        loop = asyncio.get_running_loop()
        now = loop.time()
        if self._next_frame_at is not None:
            await asyncio.sleep(max(0.0, self._next_frame_at - now))
        self._sequence, source = self._buffer.next_after(self._sequence)
        if source is None:
            samples = np.zeros(320, dtype=np.int16)
        else:
            samples = np.asarray(source.data, dtype=np.int16).reshape(-1)
        frame = AvAudioFrame.from_ndarray(
            samples.reshape(1, -1), format="s16", layout="mono"
        )
        frame.sample_rate = self._module.sample_rate
        frame.pts = self._pts
        frame.time_base = Fraction(1, self._module.sample_rate)
        duration = len(samples) / self._module.sample_rate
        self._pts += len(samples)
        self._next_frame_at = max(
            self._next_frame_at or now, now - duration
        ) + duration
        self._module.frames_sent += 1
        return frame


@dataclass
class _AudioStreamState:
    stream_id: str
    label: str
    kind: str
    buffer: _AudioBroadcastBuffer
    frames_received: int = 0


class AudioModule(MediaModule):
    """Receive browser PCM and publish host PCM to browser peers."""

    def __init__(self, sample_rate: int = 16000) -> None:
        self.sample_rate = sample_rate
        self._listeners: List[Callable[[str, AudioFrame], Any]] = []
        self._tasks: Dict[RTCPeerConnection, Set[asyncio.Task]] = {}
        self._streams: Dict[str, _AudioStreamState] = {}
        self._streams_lock = threading.RLock()
        self.frames_received = 0
        self.frames_sent = 0

    @property
    def kind(self) -> str:
        return "audio"

    def add_listener(self, callback: Callable[[str, AudioFrame], Any]) -> None:
        if not callable(callback):
            raise TypeError("audio callback must be callable")
        self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[str, AudioFrame], Any]) -> None:
        self._listeners.remove(callback)

    def register_audio_stream(
        self, stream_id: str, label: str = "", kind: str = "audio"
    ) -> None:
        stream_id = str(stream_id).strip()
        valid = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
        if not stream_id or any(char not in valid for char in stream_id):
            raise ValueError("stream_id must contain only letters, numbers, '_' or '-'")
        with self._streams_lock:
            current = self._streams.get(stream_id)
            if current is None:
                self._streams[stream_id] = _AudioStreamState(
                    stream_id,
                    label or stream_id,
                    kind or "audio",
                    _AudioBroadcastBuffer(),
                )
            else:
                if label:
                    current.label = label
                if kind:
                    current.kind = kind

    def push(
        self, frame: AudioFrame, stream_id: str = "audio", label: str = ""
    ) -> None:
        self.register_audio_stream(stream_id, label, kind="")
        with self._streams_lock:
            stream = self._streams[stream_id]
            stream.frames_received += 1
        stream.buffer.push(frame)

    def list_streams(self) -> List[dict]:
        with self._streams_lock:
            streams = list(self._streams.values())
        return [
            {
                "id": stream.stream_id,
                "label": stream.label,
                "media_type": "audio",
                "kind": stream.kind,
                "status": "live" if stream.frames_received else "waiting",
                "sample_rate": self.sample_rate,
                "channels": 1,
                "frames_received": stream.frames_received,
            }
            for stream in streams
        ]

    def attach(self, peer: RTCPeerConnection, context: PeerMediaContext) -> None:
        tasks = self._tasks.setdefault(peer, set())
        incoming_index = 0
        with self._streams_lock:
            selected = [
                self._streams[item]
                for item in context.requested_streams
                if item in self._streams
            ]
        tracks = {}
        for stream in selected:
            output_track = _AudioOutputTrack(self, stream.buffer)
            peer.addTrack(output_track)
            tracks[stream.stream_id] = output_track
        context.values["audio.selected"] = selected
        context.values["audio.tracks"] = tracks

        @peer.on("track")
        def on_track(track) -> None:
            nonlocal incoming_index
            if track.kind != self.kind:
                return
            matched = match_browser_input(peer, track, context, incoming_index)
            stream_id = matched["id"] if matched is not None else "audio_input"
            incoming_index += 1
            log.info("browser audio track attached stream=%s track=%s", stream_id, track.id)
            input_tasks = context.values.setdefault("audio.input_tasks", {})
            previous = input_tasks.get(stream_id)
            if previous is not None and not previous.done():
                previous.cancel()
            task = asyncio.create_task(self._consume(track, stream_id, context))
            input_tasks[stream_id] = task
            tasks.add(task)
            task.add_done_callback(tasks.discard)

    async def detach(self, peer: RTCPeerConnection) -> None:
        tasks = self._tasks.pop(peer, set())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _consume(self, track, stream_id: str, context: PeerMediaContext) -> None:
        resampler = AudioResampler(
            format="s16", layout="mono", rate=self.sample_rate
        )
        try:
            while True:
                source = await track.recv()
                for converted in resampler.resample(source):
                    pcm = converted.to_ndarray().reshape(-1).copy()
                    timestamp_ns = (
                        int(float(converted.time) * 1_000_000_000)
                        if converted.time is not None
                        else time.monotonic_ns()
                    )
                    frame = AudioFrame(pcm, self.sample_rate, 1, timestamp_ns)
                    self.frames_received += 1
                    handler = context.values.get("browser.audio_frame")
                    if handler is not None and handler(stream_id, frame) is False:
                        continue
                    for callback in tuple(self._listeners):
                        try:
                            if inspect.iscoroutinefunction(callback):
                                await callback(stream_id, frame)
                            else:
                                await asyncio.to_thread(callback, stream_id, frame)
                        except Exception:
                            log.exception("audio listener failed")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if track.readyState != "ended":
                log.warning("browser audio track stopped: %s", exc)
