import asyncio
import json
import time
import unittest
from fractions import Fraction
from pathlib import Path

import numpy as np

try:
    from aiohttp import ClientSession
    from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription
    from av import AudioFrame as AvAudioFrame
    from xr_media import AudioFrame, MediaServer, MediaServerConfig, VideoFrame
    from xr_media.media.track import _as_ndarray, _pad_to_even
except ImportError:
    ClientSession = None


if ClientSession is not None:
    class SilentAudioTrack(MediaStreamTrack):
        kind = "audio"

        def __init__(self):
            super().__init__()
            self.pts = 0

        async def recv(self):
            await asyncio.sleep(0.02)
            frame = AvAudioFrame.from_ndarray(
                np.zeros((1, 320), dtype=np.int16), format="s16", layout="mono"
            )
            frame.sample_rate = 16000
            frame.pts = self.pts
            frame.time_base = Fraction(1, 16000)
            self.pts += 320
            return frame


@unittest.skipIf(ClientSession is None, "aiortc/aiohttp not installed")
class WebRtcIntegrationTest(unittest.IsolatedAsyncioTestCase):
    def test_planar_formats_convert_to_av_frames(self):
        from av import VideoFrame as AvVideoFrame

        for pixel_format, av_format in (("i420", "yuv420p"), ("nv12", "nv12")):
            source = VideoFrame(bytes(24), 4, 4, pixel_format, 1)
            array = _as_ndarray(source)
            frame = AvVideoFrame.from_ndarray(array, format=av_format)
            self.assertEqual((frame.width, frame.height), (4, 4))

    def test_odd_decoded_dimensions_are_padded_without_cropping(self):
        source = VideoFrame(np.zeros((49, 65, 3), dtype=np.uint8), 65, 49)
        padded = _pad_to_even(source, _as_ndarray(source))
        self.assertEqual(padded.shape, (50, 66, 3))

    async def test_offer_answer_and_receive_frame(self):
        server = MediaServer(MediaServerConfig(
            host="127.0.0.1", port=19443,
            cert_path=Path("/tmp/webrtc-media-test-no-cert"),
            key_path=Path("/tmp/webrtc-media-test-no-key"),
        ))
        server.register_audio_stream("audio", "Test audio", "audio_test")
        server.register_browser_input("audio_input", "audio", "Test microphone")
        controls = []
        input_controls = []
        server.register_browser_input(
            "browser_video", "video", "Browser video",
            lambda action, value: input_controls.append((action, value)),
        )
        server.register_video_stream(
            "main", "Main file", kind="file",
            on_control=lambda action, value: controls.append((action, value)),
        )
        server.set_stream_playback(
            "main", position=0.0, duration=3.0, paused=False, ended=False, loop=False
        )
        image = np.zeros((49, 65, 3), dtype=np.uint8)
        image[:, :, 1] = 200
        server.push_video_frame(
            VideoFrame(image, 65, 49, "bgr24", time.monotonic_ns()),
            "main", "Main file",
        )
        second = np.zeros((24, 32, 3), dtype=np.uint8)
        second[:, :, 2] = 200
        server.push_video_frame(
            VideoFrame(second, 32, 24, "bgr24", time.monotonic_ns()),
            "wrist", "Wrist camera",
        )
        await server.start()
        pc = RTCPeerConnection()
        received = []
        all_received = asyncio.get_running_loop().create_future()
        audio_received = asyncio.Event()
        audio_frames = []
        output_audio_received = asyncio.Event()
        async def on_audio(_stream_id, frame):
            audio_frames.append(frame)
            audio_received.set()

        server.add_audio_listener(on_audio)

        @pc.on("track")
        async def on_track(track):
            try:
                if track.kind == "audio":
                    for _ in range(20):
                        frame = await asyncio.wait_for(track.recv(), 5)
                        if np.any(frame.to_ndarray()):
                            output_audio_received.set()
                            return
                    return
                frame = await asyncio.wait_for(track.recv(), 5)
                received.append(frame)
                if len(received) == 2 and not all_received.done():
                    all_received.set_result(received)
            except Exception as exc:
                if not all_received.done():
                    all_received.set_exception(exc)

        try:
            pc.addTransceiver("video", direction="recvonly")
            pc.addTransceiver("video", direction="recvonly")
            pc.addTrack(SilentAudioTrack())
            offer = await pc.createOffer()
            await pc.setLocalDescription(offer)
            async with ClientSession() as session:
                async with session.ws_connect("http://127.0.0.1:19443/signal") as ws:
                    await ws.send_str(json.dumps({
                        "type": "input.control", "stream": "audio_input",
                        "action": "start", "status": "live",
                    }))
                    await ws.send_str(json.dumps({
                        "type": pc.localDescription.type,
                        "sdp": pc.localDescription.sdp,
                        "streams": ["main", "wrist", "audio"],
                    }))
                    answer = json.loads((await ws.receive()).data)
                    self.assertEqual(
                        [item["label"] for item in answer["streams"]],
                        ["Main file", "Wrist camera", "Test audio"],
                    )
                    self.assertEqual(
                        [item["media_type"] for item in answer["streams"]],
                        ["video", "video", "audio"],
                    )
                    await pc.setRemoteDescription(
                        RTCSessionDescription(sdp=answer["sdp"], type=answer["type"])
                    )
                    for _ in range(3):
                        await asyncio.sleep(0.05)
                        server.push_video_frame(
                            VideoFrame(image, 65, 49, "bgr24", time.monotonic_ns()),
                            "main", "Main file",
                        )
                        server.push_video_frame(
                            VideoFrame(second, 32, 24, "bgr24", time.monotonic_ns()),
                            "wrist", "Wrist camera",
                        )
                    server.push_audio_frame(
                        AudioFrame(
                            np.full(320, 5000, dtype=np.int16),
                            16000,
                            1,
                            time.monotonic_ns(),
                        )
                    )
                    frames = await asyncio.wait_for(all_received, 8)
                    await asyncio.wait_for(audio_received.wait(), 8)
                    await asyncio.wait_for(output_audio_received.wait(), 8)
                    self.assertEqual(
                        {(frame.width, frame.height) for frame in frames},
                        {(66, 50), (32, 24)},
                    )
                    self.assertEqual(audio_frames[0].sample_rate, 16000)
                    self.assertEqual(audio_frames[0].channels, 1)
                    self.assertGreater(audio_frames[0].samples, 0)
                    await ws.send_str(json.dumps({
                        "type": "stream.control", "stream": "main",
                        "action": "loop", "value": True,
                    }))
                    await ws.send_str(json.dumps({
                        "type": "input.control", "stream": "browser_video",
                        "action": "start", "value": "device", "status": "live",
                    }))
                    await ws.send_str(json.dumps({
                        "type": "input.control", "stream": "browser_video",
                        "action": "preview", "value": True, "status": "live",
                    }))
                    await asyncio.sleep(0.05)
                    self.assertEqual(controls, [("loop", True)])
                    self.assertEqual(
                        input_controls, [("start", "device"), ("preview", True)]
                    )
                    self.assertEqual(
                        server._browser_inputs["browser_video"]["status"], "live"
                    )
        finally:
            await pc.close()
            await server.stop()


if __name__ == "__main__":
    unittest.main()
