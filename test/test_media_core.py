import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
from aiortc import RTCBundlePolicy
import xr_media
from xr_media.media import MediaServer
from xr_media.media.audio import AudioModule
from xr_media.media.buffer import LatestFrameBuffer
from xr_media.media.frame import AudioFrame, MediaServerConfig, VideoFrame
from xr_media.media.media import MediaModule
from xr_media.media.track import BufferedVideoTrack
from xr_media.media.video import VideoModule


class CoreTest(unittest.TestCase):
    def test_typed_stream_api_preserves_kind_when_pushing(self):
        server = MediaServer()
        server.register_video_stream("movie", kind="file")
        server.register_audio_stream("sound", kind="audio_test")
        self.assertIsNone(server.push_video_frame(VideoFrame(bytes(12), 2, 2), "movie"))
        self.assertIsNone(server.push_audio_frame(AudioFrame([1, 2, 3]), "sound"))
        self.assertEqual(server.list_streams()[0]["kind"], "file")
        self.assertEqual(server.audio.list_streams()[0]["kind"], "audio_test")

    def test_stream_kinds_default_and_accept_custom_labels(self):
        server = MediaServer()
        server.push_video_frame(VideoFrame(bytes(12), 2, 2))
        server.push_audio_frame(AudioFrame([1]))
        self.assertEqual(server.list_streams()[0]["kind"], "live")
        self.assertEqual(server.audio.list_streams()[0]["kind"], "audio")
        server.register_video_stream("main", kind="custom")
        server.register_video_stream("main")
        server.register_audio_stream("audio", kind="custom")
        server.push_audio_frame(AudioFrame([2]))
        self.assertEqual(server.list_streams()[0]["kind"], "custom")
        self.assertEqual(server.audio.list_streams()[0]["kind"], "custom")

    def test_old_stream_api_names_are_absent(self):
        server = MediaServer()
        for name in ("register_stream", "push_frame", "push_audio"):
            self.assertFalse(hasattr(server, name))

    def test_bundle_policy_preserves_configured_ice_servers(self):
        server = MediaServer(MediaServerConfig(ice_servers=[
            ("turn:localhost:3478", "test-user", "test-password")
        ]))
        configuration = server._ice_configuration()
        self.assertEqual(configuration.bundlePolicy, RTCBundlePolicy.MAX_BUNDLE)
        self.assertEqual(configuration.iceServers[0].urls, "turn:localhost:3478")
        self.assertEqual(configuration.iceServers[0].username, "test-user")
        self.assertEqual(configuration.iceServers[0].credential, "test-password")

    def test_stop_closes_signaling_websockets(self):
        server = MediaServer()
        socket = MagicMock()
        socket.close = AsyncMock()
        server._signal_peers.add(socket)

        asyncio.run(server.stop())

        socket.close.assert_awaited_once_with(
            code=1001, message=b"server shutdown"
        )
        self.assertFalse(server._signal_peers)

    def test_latest_frame_replaces_stale_data(self):
        frames = LatestFrameBuffer()
        first = VideoFrame(bytes(12), 2, 2, "bgr24", 1)
        second = VideoFrame(bytes(12), 2, 2, "bgr24", 2)
        frames.push(first)
        frames.push(second)
        current, sequence = frames.latest()
        self.assertEqual(current.timestamp_ns, 2)
        self.assertEqual(sequence, 2)
        self.assertEqual(frames.counts(), (2, 1))

    def test_live_video_timestamps_follow_arrival_and_ignore_pause_gap(self):
        async def exercise():
            track = BufferedVideoTrack(LatestFrameBuffer(), lambda: None)
            first, _ = await track._next_timestamp(1_000_000_000)
            second, _ = await track._next_timestamp(1_033_333_333)
            resumed, _ = await track._next_timestamp(6_033_333_333)
            return first, second, resumed

        first, second, resumed = asyncio.run(exercise())
        self.assertEqual(first, 0)
        self.assertAlmostEqual(second, 3000, delta=1)
        self.assertAlmostEqual(resumed - second, 3000, delta=1)

    def test_new_frames_send_without_pacing(self):
        async def exercise():
            frames = LatestFrameBuffer()
            frames.push(VideoFrame(np.zeros((2, 2, 3), dtype=np.uint8), 2, 2))
            track = BufferedVideoTrack(frames, lambda: None)
            frames.push(VideoFrame(np.full((2, 2, 3), 180, dtype=np.uint8), 2, 2))
            with patch("xr_media.media.track.asyncio.sleep") as sleep:
                result = await track.recv()
                sleep.assert_not_called()
            self.assertEqual(int(result.to_ndarray(format="bgr24")[0, 0, 0]), 180)
            self.assertEqual(track._last_sequence, 2)
            with patch("xr_media.media.track.asyncio.sleep") as sleep:
                for timestamp in range(3, 8):
                    frames.push(VideoFrame(bytes(12), 2, 2, timestamp_ns=timestamp))
                    await track.recv()
                sleep.assert_not_called()
            track.stop()
        asyncio.run(exercise())

    def test_video_waits_for_new_frame(self):
        async def exercise():
            frames = LatestFrameBuffer()
            track = BufferedVideoTrack(frames, lambda: None)
            frames.push(VideoFrame(bytes(12), 2, 2))
            await track.recv()
            pending = asyncio.create_task(track.recv())
            try:
                await asyncio.sleep(0.01)
                self.assertFalse(pending.done())
                frames.push(VideoFrame(bytes(12), 2, 2))
                await asyncio.wait_for(pending, 1)
            finally:
                pending.cancel()
                track.stop()
        asyncio.run(exercise())

    def test_teleimager_bitrate_profile(self):
        from aiortc.codecs import h264, vpx
        original = [
            (codec, codec.MIN_BITRATE, codec.DEFAULT_BITRATE, codec.MAX_BITRATE)
            for codec in (h264, vpx)
        ]
        try:
            VideoModule(MediaServerConfig(), lambda: None)
            for codec in (h264, vpx):
                self.assertEqual(
                    (codec.MIN_BITRATE, codec.DEFAULT_BITRATE, codec.MAX_BITRATE),
                    (1000000, 3000000, 6000000),
                )
            with self.assertRaises(ValueError):
                MediaServerConfig(video_bitrate_max=100000)
        finally:
            for codec, low, default, high in original:
                codec.MIN_BITRATE, codec.DEFAULT_BITRATE, codec.MAX_BITRATE = low, default, high

    def test_config_validation(self):
        self.assertEqual(MediaServerConfig().codec, "h264")
        with self.assertRaises(TypeError):
            MediaServerConfig(fps=0)
        with self.assertRaises(ValueError):
            MediaServerConfig(codec="mpeg2")

    def test_frame_validation(self):
        with self.assertRaises(ValueError):
            VideoFrame(b"", 0, 10)
        with self.assertRaises(ValueError):
            VideoFrame(b"", 10, 10, "jpeg")

    def test_audio_and_video_use_media_api_without_legacy_server_names(self):
        self.assertTrue(issubclass(AudioModule, MediaModule))
        self.assertTrue(issubclass(VideoModule, MediaModule))
        self.assertFalse(hasattr(xr_media, "VideoServer"))
        pcm = AudioFrame([1, 2, 3])
        self.assertEqual(pcm.samples, 3)
        with self.assertRaises(ValueError):
            AudioFrame([1], sample_rate=48000)
        with self.assertRaises(ValueError):
            AudioFrame([1], channels=2)

    def test_browser_inputs_are_explicit_and_keep_independent_controls(self):
        server = MediaServer()
        video_controls = []
        audio_controls = []
        server.register_browser_input(
            "remote_video", "video", "Remote video",
            lambda action, value: video_controls.append((action, value)),
        )
        server.register_browser_input(
            "remote_audio", "audio", "Remote audio",
            lambda action, value: audio_controls.append((action, value)),
        )
        self.assertEqual(server._browser_inputs["remote_video"]["media_type"], "video")
        self.assertEqual(server._browser_inputs["remote_audio"]["media_type"], "audio")
        self.assertIsNot(
            server._browser_inputs["remote_video"]["control"],
            server._browser_inputs["remote_audio"]["control"],
        )
        server._browser_inputs["remote_audio"]["preview"] = True
        server._browser_inputs["remote_audio"]["status"] = "live"
        server._play_input_audio_frame("remote_audio", AudioFrame([100, -100, 50]))
        self.assertEqual(
            server._input_audio_samples["remote_audio"], [100, -100, 50]
        )
        self.assertEqual(
            server._browser_inputs["remote_audio"]["frames_received"], 1
        )
        server._browser_inputs["remote_audio"]["status"] = "idle"
        accepted = server._play_input_audio_frame(
            "remote_audio", AudioFrame([200, -200, 100])
        )
        self.assertFalse(accepted)
        self.assertEqual(
            server._browser_inputs["remote_audio"]["frames_received"], 1
        )

    def test_pc_video_preview_uses_jpeg(self):
        image = np.zeros((24, 32, 3), dtype=np.uint8)
        encoded = MediaServer._encode_input_preview(image)
        self.assertTrue(encoded.startswith(b"\xff\xd8"))
        self.assertTrue(encoded.endswith(b"\xff\xd9"))

    @patch("xr_media.media.server.time.sleep")
    @patch("xr_media.media.server.subprocess.Popen")
    @patch("xr_media.media.server.shutil.which")
    def test_pc_out_falls_back_when_pulse_cannot_start(self, which, popen, _sleep):
        which.side_effect = lambda name: "/usr/bin/" + name
        failed = MagicMock()
        failed.poll.return_value = 1
        failed.communicate.return_value = (b"", b"pulse unavailable")
        active = MagicMock()
        active.poll.return_value = None
        popen.side_effect = [failed, active]
        server = MediaServer()

        self.assertTrue(server._set_input_speaker("remote_audio", True))
        self.assertIs(server._audio_players["remote_audio"], active)
        self.assertEqual(popen.call_count, 2)


if __name__ == "__main__":
    unittest.main()
