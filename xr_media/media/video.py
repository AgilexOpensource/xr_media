"""Host-to-browser WebRTC video module."""

from collections import deque
from dataclasses import dataclass, field
import threading
import time
import asyncio
import inspect
import logging
from typing import Any, Callable, Dict, List, Optional, Set

from aiortc import RTCPeerConnection
from aiortc.rtcrtpsender import RTCRtpSender

from .buffer import LatestFrameBuffer
from .frame import MediaServerConfig, VideoFrame
from .media import MediaModule, PeerMediaContext, match_browser_input
from .track import BufferedVideoTrack

log = logging.getLogger("xr_media.media.video")


@dataclass
class _StreamState:
    stream_id: str
    label: str
    frames: LatestFrameBuffer
    status: str = "waiting"
    kind: str = "live"
    control: Optional[Callable[[str, Any], None]] = None
    playback: Optional[dict] = None
    capture_times: deque = field(default_factory=deque)


class VideoModule(MediaModule):
    def __init__(
        self, config: MediaServerConfig, on_frame_sent: Callable[[], None]
    ) -> None:
        self.config = config
        # Match TeleImager _apply_webrtc_config. These are aiortc module-level
        # controls, not per-peer settings; the last configured server wins.
        from aiortc.codecs import h264, vpx
        for codec in (h264, vpx):
            codec.MIN_BITRATE = config.video_bitrate_min
            codec.DEFAULT_BITRATE = config.video_bitrate_default
            codec.MAX_BITRATE = config.video_bitrate_max
        self.on_frame_sent = on_frame_sent
        self.streams: Dict[str, _StreamState] = {}
        self.lock = threading.RLock()
        self._listeners: List[Callable[[str, VideoFrame], Any]] = []
        self._tasks: Dict[RTCPeerConnection, Set[asyncio.Task]] = {}

    @property
    def kind(self) -> str:
        return "video"

    def add_listener(self, callback: Callable[[str, VideoFrame], Any]) -> None:
        if not callable(callback):
            raise TypeError("video callback must be callable")
        self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[str, VideoFrame], Any]) -> None:
        self._listeners.remove(callback)

    def register_video_stream(
        self,
        stream_id: str,
        label: str = "",
        kind: str = "",
        on_control: Optional[Callable[[str, Any], None]] = None,
    ) -> None:
        stream_id = str(stream_id).strip()
        valid = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
        if not stream_id or any(char not in valid for char in stream_id):
            raise ValueError("stream_id must contain only letters, numbers, '_' or '-'")
        with self.lock:
            current = self.streams.get(stream_id)
            if current is None:
                self.streams[stream_id] = _StreamState(
                    stream_id,
                    label or stream_id,
                    LatestFrameBuffer(),
                    kind=kind or "live",
                    control=on_control,
                    playback={} if kind == "file" else None,
                )
                return
            if label:
                current.label = label
            if kind:
                current.kind = kind
            if on_control is not None:
                current.control = on_control

    def push_video_frame(
        self, frame: VideoFrame, stream_id: str = "main", label: str = ""
    ) -> None:
        self.register_video_stream(stream_id, label)
        with self.lock:
            stream = self.streams[stream_id]
            now = time.monotonic()
            stream.capture_times.append(now)
            while stream.capture_times and stream.capture_times[0] < now - 2.0:
                stream.capture_times.popleft()
        stream.frames.push(frame)
        stream.status = "live"

    def set_stream_status(self, stream_id: str, status: str) -> None:
        if status not in {"waiting", "live", "paused", "seeking", "ended", "error"}:
            raise ValueError("invalid stream status: %s" % status)
        with self.lock:
            if stream_id not in self.streams:
                raise KeyError(stream_id)
            self.streams[stream_id].status = status

    def set_stream_playback(self, stream_id: str, **values) -> None:
        with self.lock:
            if stream_id not in self.streams:
                raise KeyError(stream_id)
            stream = self.streams[stream_id]
            if stream.playback is None:
                stream.playback = {}
            stream.playback.update(values)

    def list_streams(self) -> List[dict]:
        with self.lock:
            states = list(self.streams.values())
        result = []
        for state in states:
            frame, _ = state.frames.latest()
            received, replaced = state.frames.counts()
            with self.lock:
                times = tuple(state.capture_times)
            capture_fps = (
                (len(times) - 1) / (times[-1] - times[0])
                if len(times) > 1 and times[-1] > times[0]
                else 0.0
            )
            result.append({
                "id": state.stream_id,
                "label": state.label,
                "media_type": "video",
                "ready": frame is not None,
                "status": state.status,
                "kind": state.kind,
                "playback": dict(state.playback or {}),
                "width": frame.width if frame else 0,
                "height": frame.height if frame else 0,
                "frames_received": received,
                "frames_replaced": replaced,
                "capture_fps": capture_fps,
            })
        return result

    def counts(self):
        with self.lock:
            states = list(self.streams.values())
        counts = [state.frames.counts() for state in states]
        snapshots = [state.frames.latest()[0] for state in states]
        return states, counts, snapshots

    def attach(self, peer: RTCPeerConnection, context: PeerMediaContext) -> None:
        tasks = self._tasks.setdefault(peer, set())
        incoming_index = 0
        with self.lock:
            selected = [
                self.streams[item]
                for item in context.requested_streams
                if item in self.streams
            ]
        tracks = {}
        for stream in selected:
            track = BufferedVideoTrack(
                stream.frames,
                self.on_frame_sent,
                should_send=lambda current=stream: current.status
                not in {"paused", "ended", "error"},
            )
            tracks[stream.stream_id] = track
            peer.addTrack(track)
        wanted = "video/" + self.config.codec.lower()
        codecs = [
            codec
            for codec in RTCRtpSender.getCapabilities("video").codecs
            if codec.mimeType.lower() == wanted
        ]
        if codecs:
            for transceiver in peer.getTransceivers():
                if transceiver.kind == self.kind:
                    transceiver.setCodecPreferences(codecs)
        context.values["video.selected"] = selected
        context.values["video.tracks"] = tracks

        @peer.on("track")
        def on_track(track) -> None:
            nonlocal incoming_index
            if track.kind != self.kind:
                return
            matched = match_browser_input(peer, track, context, incoming_index)
            stream_id = matched["id"] if matched is not None else "video_input"
            log.info("browser video track attached stream=%s track=%s", stream_id, track.id)
            incoming_index += 1
            input_tasks = context.values.setdefault("video.input_tasks", {})
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
        latest = asyncio.Queue(maxsize=1)
        stopped = object()

        async def receive_latest() -> None:
            try:
                while True:
                    source = await track.recv()
                    if latest.full():
                        latest.get_nowait()
                    latest.put_nowait(source)
                    # Let conversion run even while aiortc already has queued frames.
                    await asyncio.sleep(0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if track.readyState != "ended":
                    log.warning("browser video track stopped: %s", exc)
            finally:
                if latest.full():
                    latest.get_nowait()
                latest.put_nowait(stopped)

        receiver = asyncio.create_task(receive_latest())
        try:
            while True:
                source = await latest.get()
                if source is stopped:
                    break
                # Conversion can be expensive for mobile resolutions. Running it
                # outside the event loop lets the receiver keep only the newest
                # decoded frame instead of accumulating seconds of stale video.
                image = await asyncio.to_thread(source.to_ndarray, format="bgr24")
                timestamp_ns = (
                    int(float(source.time) * 1_000_000_000)
                    if source.time is not None else time.monotonic_ns()
                )
                frame = VideoFrame(
                    image, source.width, source.height, "bgr24", timestamp_ns
                )
                handler = context.values.get("browser.video_frame")
                if handler is not None and handler(stream_id, frame) is False:
                    continue
                for callback in tuple(self._listeners):
                    try:
                        if inspect.iscoroutinefunction(callback):
                            await callback(stream_id, frame)
                        else:
                            await asyncio.to_thread(callback, stream_id, frame)
                    except Exception:
                        log.exception("video listener failed")
        finally:
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)
