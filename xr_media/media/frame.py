"""Media frame and server configuration types with no ROS dependency."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence, Tuple


@dataclass(frozen=True)
class VideoFrame:
    """One decoded image.

    ``data`` may be a NumPy array or tightly packed bytes. Supported formats are
    bgr24, rgb24, gray8, i420 and nv12.
    """

    data: Any
    width: int
    height: int
    pixel_format: str = "bgr24"
    timestamp_ns: int = 0

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("frame width and height must be positive")
        if self.pixel_format not in {"bgr24", "rgb24", "gray8", "i420", "nv12"}:
            raise ValueError("unsupported pixel format: %s" % self.pixel_format)


@dataclass(frozen=True)
class AudioFrame:
    """One PCM block used by browser input and browser output.

    ``data`` is a one-dimensional NumPy array containing signed 16-bit mono
    samples. Browser audio is resampled to 16 kHz before delivery.
    """

    data: Any
    sample_rate: int = 16000
    channels: int = 1
    timestamp_ns: int = 0

    def __post_init__(self) -> None:
        if self.sample_rate != 16000:
            raise ValueError("audio sample_rate must be 16000")
        if self.channels != 1:
            raise ValueError("audio channels must be 1")
        if self.samples <= 0:
            raise ValueError("audio frame must contain samples")

    @property
    def samples(self) -> int:
        return int(getattr(self.data, "size", len(self.data)))


@dataclass(frozen=True)
class MediaServerConfig:
    host: str = "0.0.0.0"
    port: int = 9443
    cert_path: Optional[Path] = None
    key_path: Optional[Path] = None
    codec: str = "h264"
    ice_servers: Sequence[Tuple[str, Optional[str], Optional[str]]] = ()
    max_clients: int = 1
    # TeleImager LAN profile, bits/s. aiortc applies these process-wide.
    video_bitrate_min: int = 1_000_000
    video_bitrate_default: int = 3_000_000
    video_bitrate_max: int = 6_000_000

    def __post_init__(self) -> None:
        if not 1 <= int(self.port) <= 65535:
            raise ValueError("port must be between 1 and 65535")
        if not (100_000 <= self.video_bitrate_min <= self.video_bitrate_default <= self.video_bitrate_max):
            raise ValueError("video bitrates must satisfy 100000 <= min <= default <= max")
        if int(self.max_clients) < 1:
            raise ValueError("max_clients must be positive")
        if self.codec.lower() not in {"h264", "vp8", "vp9"}:
            raise ValueError("codec must be h264, vp8 or vp9")


@dataclass(frozen=True)
class MediaServerStats:
    clients: int
    frames_received: int
    frames_replaced: int
    frames_sent: int
    latest_timestamp_ns: int
    streams: int = 0
    audio_frames_received: int = 0
    audio_frames_sent: int = 0
