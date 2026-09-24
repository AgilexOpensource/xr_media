"""Integrated WebRTC media and WebXR SDK."""

from .media import AudioFrame, MediaServerConfig, MediaServerStats, VideoFrame
from .xr import HandInput, RigidPose, TrackedHand, XrSample

__all__ = [
    "AudioFrame", "HandInput", "MediaServer", "MediaServerConfig",
    "MediaServerStats", "RigidPose", "TrackedHand",
    "XrSample", "VideoFrame", "XrMediaServer",
]


def __getattr__(name):
    if name == "MediaServer":
        from .media import MediaServer

        return MediaServer
    if name == "XrMediaServer":
        from .server import XrMediaServer

        return XrMediaServer
    raise AttributeError(name)
