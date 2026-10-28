import fractions
import re
import unittest

import av
import numpy as np
from aiortc.codecs import h264

from xr_media.media import x264
from xr_media.media.frame import MediaServerConfig
from xr_media.media.video import VideoModule


def x264_settings(encoder, width=640, height=480, pts=0):
    # PyAV clears codec.options after open; x264 reports its effective
    # settings in an SEI NAL of the first frame instead.
    frame = av.VideoFrame.from_ndarray(
        np.zeros((height, width, 3), np.uint8), format="bgr24")
    frame.pts = pts
    frame.time_base = fractions.Fraction(1, 90000)
    payloads, _timestamp = encoder.encode(frame)
    match = re.search(rb"options: ([^\x00]*)", b"".join(payloads))
    if not match:
        raise AssertionError("x264 SEI not found")
    return dict(item.split("=", 1) for item in match.group(1).decode().split() if "=" in item)


class X264Test(unittest.TestCase):
    def tearDown(self):
        x264.configure_x264("", 0)

    def test_server_default_is_ultrafast_single_thread(self):
        VideoModule(MediaServerConfig(), lambda: None)
        settings = x264_settings(h264.H264Encoder())
        self.assertEqual(settings["threads"], "1")
        self.assertEqual((settings["me"], settings["subme"]), ("dia", "0"))
        self.assertEqual(settings["bframes"], "0")  # tune=zerolatency kept

    def test_codec_recreated_on_resize_keeps_options(self):
        x264.configure_x264("ultrafast", 1)
        encoder = h264.H264Encoder()
        x264_settings(encoder)
        first = encoder.codec
        settings = x264_settings(encoder, 1280, 720, pts=3000)
        self.assertIsNot(encoder.codec, first)
        self.assertEqual(encoder.codec.width, 1280)
        self.assertEqual((settings["threads"], settings["subme"]), ("1", "0"))

    def test_preset_and_threads_are_independent(self):
        x264.configure_x264("", 2)
        settings = x264_settings(h264.H264Encoder())
        self.assertEqual(settings["threads"], "2")
        self.assertNotEqual(settings["subme"], "0")

    def test_empty_options_restore_aiortc_encoder(self):
        x264.configure_x264("ultrafast", 1)
        VideoModule(MediaServerConfig(video_x264_preset="", video_x264_threads=0), lambda: None)
        self.assertIs(h264.H264Encoder._encode_frame, x264._original_encode_frame)
        self.assertNotEqual(x264_settings(h264.H264Encoder())["subme"], "0")

    def test_invalid_options_rejected(self):
        with self.assertRaises(ValueError):
            MediaServerConfig(video_x264_preset="turbo")
        with self.assertRaises(ValueError):
            MediaServerConfig(video_x264_threads=-1)
        with self.assertRaises(ValueError):
            x264.configure_x264("turbo", 1)


if __name__ == "__main__":
    unittest.main()
