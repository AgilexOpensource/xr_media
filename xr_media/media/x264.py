"""libx264 option control for aiortc's H.264 encoder.

aiortc creates libx264 with only ``level=31, tune=zerolatency`` (x264 preset
"medium", automatic threads) and exposes no hook for other options. This module
wraps ``H264Encoder._encode_frame`` to pre-create the codec with the same
settings plus the configured preset/threads; aiortc then reuses that codec.

On a PICO 4 Ultra at 640x480, ``ultrafast`` + 1 thread cut encode time from
~7.5 ms to ~2.3 ms and end-to-end latency by ~20 ms.
"""

import fractions
from typing import Dict

import av
from aiortc.codecs import h264

from .frame import X264_PRESETS

_original_encode_frame = h264.H264Encoder._encode_frame
_extra_options: Dict[str, str] = {}


def _needs_new_codec(encoder, frame) -> bool:
    # same reset rule as aiortc: size change or >10% bitrate change
    codec = encoder.codec
    return (
        frame.width != codec.width
        or frame.height != codec.height
        or abs(encoder.target_bitrate - codec.bit_rate) / codec.bit_rate > 0.1
    )


def _encode_frame(self, frame, force_keyframe):
    if self.codec is not None and _needs_new_codec(self, frame):
        self.buffer_data = b""
        self.buffer_pts = None
        self.codec = None
    if self.codec is None:
        codec = av.CodecContext.create("libx264", "w")
        codec.width = frame.width
        codec.height = frame.height
        codec.bit_rate = self.target_bitrate
        codec.pix_fmt = "yuv420p"
        codec.framerate = fractions.Fraction(h264.MAX_FRAME_RATE, 1)
        codec.time_base = fractions.Fraction(1, h264.MAX_FRAME_RATE)
        codec.options = {"level": "31", "tune": "zerolatency", **_extra_options}
        codec.profile = "Baseline"
        self.codec = codec
    return _original_encode_frame(self, frame, force_keyframe)


def configure_x264(preset: str, threads: int) -> None:
    """Apply x264 options to H.264 encoders created afterwards (process-wide).

    ``preset=""`` keeps the x264 default and ``threads=0`` keeps automatic
    threading; with both, aiortc's original encoder path is restored.
    """
    if preset not in X264_PRESETS:
        raise ValueError("video_x264_preset must be one of: %s" % ", ".join(
            repr(name) for name in X264_PRESETS))
    if int(threads) < 0:
        raise ValueError("video_x264_threads must be >= 0")
    _extra_options.clear()
    if preset:
        _extra_options["preset"] = preset
    if threads:
        _extra_options["threads"] = str(int(threads))
    h264.H264Encoder._encode_frame = (
        _encode_frame if _extra_options else _original_encode_frame
    )
