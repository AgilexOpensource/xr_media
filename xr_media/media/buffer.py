"""Thread-safe latest-frame buffer."""

import threading
import time
from typing import Optional, Tuple

from .frame import VideoFrame


class LatestFrameBuffer:
    """Capacity-one buffer; replacing stale frames keeps latency bounded."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._frame: Optional[VideoFrame] = None
        self._sequence = 0
        self._arrival_ns = 0
        self.received = 0
        self.replaced = 0

    def push(self, frame: VideoFrame) -> int:
        with self._lock:
            self.received += 1
            if self._frame is not None:
                self.replaced += 1
            self._frame = frame
            self._sequence += 1
            self._arrival_ns = time.monotonic_ns()
            return self._sequence

    def latest(self) -> Tuple[Optional[VideoFrame], int]:
        with self._lock:
            return self._frame, self._sequence

    def latest_timed(self) -> Tuple[Optional[VideoFrame], int, int]:
        """Return the newest frame and its local monotonic arrival time."""
        with self._lock:
            return self._frame, self._sequence, self._arrival_ns

    def counts(self) -> Tuple[int, int]:
        with self._lock:
            return self.received, self.replaced
