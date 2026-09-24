"""ROS-independent WebRTC audio and video SDK."""

from .frame import (
    AudioFrame,
    MediaServerConfig,
    MediaServerStats,
    VideoFrame,
)

__all__ = [
    "AudioFrame",
    "MediaServer",
    "MediaServerConfig",
    "MediaServerStats",
    "VideoFrame",
]


def __getattr__(name):
    if name == "MediaServer":
        from .server import MediaServer

        return MediaServer
    raise AttributeError(name)
