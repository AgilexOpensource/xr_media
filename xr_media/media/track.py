"""aiortc video track backed by a capacity-one frame buffer."""

import asyncio
from fractions import Fraction
from typing import Callable, Optional

import numpy as np
from av import VideoFrame as AvVideoFrame
from aiortc import VideoStreamTrack

from .buffer import LatestFrameBuffer
from .frame import VideoFrame


def _as_ndarray(frame: VideoFrame) -> np.ndarray:
    if isinstance(frame.data, np.ndarray):
        return frame.data
    raw = np.frombuffer(frame.data, dtype=np.uint8)
    if frame.pixel_format in {"bgr24", "rgb24"}:
        return raw.reshape((frame.height, frame.width, 3))
    if frame.pixel_format == "gray8":
        return raw.reshape((frame.height, frame.width))
    if frame.pixel_format in {"i420", "nv12"}:
        return raw.reshape((frame.height * 3 // 2, frame.width))
    raise ValueError("unsupported pixel format: %s" % frame.pixel_format)


def _pad_to_even(frame: VideoFrame, array: np.ndarray) -> np.ndarray:
    """Pad decoded image edges so YUV420/H.264 can encode arbitrary sizes."""
    pad_width = frame.width % 2
    pad_height = frame.height % 2
    if not pad_width and not pad_height:
        return array
    if frame.pixel_format in {"i420", "nv12"}:
        raise ValueError(
            "%s input requires even width and height" % frame.pixel_format
        )
    padding = ((0, pad_height), (0, pad_width))
    if array.ndim == 3:
        padding += ((0, 0),)
    return np.pad(array, padding, mode="edge")


class BufferedVideoTrack(VideoStreamTrack):
    def __init__(
        self,
        frames: LatestFrameBuffer,
        on_sent: Callable[[], None],
        should_send: Optional[Callable[[], bool]] = None,
    ) -> None:
        super().__init__()
        self._frames = frames
        self._on_sent = on_sent
        self._should_send = should_send or (lambda: True)
        self._last_sequence = -1
        self._last_frame = None
        self._timestamp = None
        self._last_arrival_ns = None
        self._arrival_step = 3000
        self._active = asyncio.Event()
        self._active.set()

    def pause(self) -> None:
        self._active.clear()

    def resume(self) -> None:
        self._active.set()

    async def _next_timestamp(self, arrival_ns: Optional[int] = None):
        """Generate locally-timed RTP timestamps.

        Source timestamps can jump when a browser track is paused, replaced, or
        renegotiated.  A fresh RTP sender must therefore use the time at which
        frames reached this buffer rather than replaying the source RTP clock.
        """
        loop = asyncio.get_running_loop()
        now_ns = arrival_ns or int(loop.time() * 1_000_000_000)
        if self._last_arrival_ns is None:
            self._timestamp = 0
        else:
            delta_ns = now_ns - self._last_arrival_ns
            if 0 < delta_ns <= 250_000_000:
                step = max(1, round(delta_ns * 90000 / 1e9))
                self._arrival_step = step
            else:
                step = self._arrival_step
            self._timestamp += step
        self._last_arrival_ns = now_ns
        return self._timestamp, Fraction(1, 90000)

    async def recv(self) -> AvVideoFrame:
        await self._active.wait()
        while self._last_frame is not None and not self._should_send():
            await asyncio.sleep(0.1)
            await self._active.wait()
        while True:
            frame, sequence, arrival_ns = self._frames.latest_timed()
            if frame is not None and sequence != self._last_sequence:
                self._last_sequence = sequence
                self._last_frame = frame
                break
            await asyncio.sleep(0.002)

        pts, time_base = await self._next_timestamp(arrival_ns)
        array = _pad_to_even(frame, _as_ndarray(frame))
        av_format = "yuv420p" if frame.pixel_format == "i420" else frame.pixel_format
        av_frame = AvVideoFrame.from_ndarray(array, format=av_format)
        av_frame.pts = pts
        av_frame.time_base = time_base or Fraction(1, 90000)
        self._on_sent()
        return av_frame
