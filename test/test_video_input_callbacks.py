import asyncio
from types import SimpleNamespace
import unittest

import numpy as np
from av import VideoFrame as AvVideoFrame

from xr_media import MediaServer, VideoFrame
from xr_media.media.media import PeerMediaContext


class VideoInputCallbacksTest(unittest.IsolatedAsyncioTestCase):
    async def test_receive_chain_preserves_gate_and_callback_errors(self):
        for mode in ("accepted", "rejected", "callback_error"):
            with self.subTest(mode=mode):
                server = MediaServer()
                context = PeerMediaContext([])
                handled = asyncio.Event()
                callbacks = []

                def gate(stream_id, frame):
                    if mode == "rejected":
                        handled.set()
                    return mode != "rejected"

                context.values["browser.video_frame"] = gate
                source = AvVideoFrame.from_ndarray(np.zeros((16, 16, 3), dtype=np.uint8), format="bgr24")
                emitted = False

                async def recv():
                    nonlocal emitted
                    if not emitted:
                        emitted = True
                        return source
                    await asyncio.Future()

                async def callback(stream_id, frame):
                    callbacks.append(stream_id)
                    handled.set()
                    if mode == "callback_error":
                        raise ValueError("callback failed")

                server.add_video_listener(callback)
                track = SimpleNamespace(id="track", recv=recv, readyState="live")
                task = asyncio.create_task(server.video._consume(track, "input", context))
                try:
                    await asyncio.wait_for(handled.wait(), 2)
                    await asyncio.sleep(0)
                    self.assertEqual(callbacks, [] if mode == "rejected" else ["input"])
                    self.assertFalse(task.done())
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_live_video_counters(self):
        server = MediaServer()
        server.register_browser_input("input", "video", "Input")
        frame = VideoFrame(np.zeros((2, 2, 3), dtype=np.uint8), 2, 2, "bgr24", 123)
        self.assertFalse(server._store_input_video_frame("input", frame))
        server._browser_inputs["input"]["status"] = "live"
        self.assertTrue(server._store_input_video_frame("input", frame))
        self.assertEqual(server._browser_inputs["input"]["frames_received"], 1)
        self.assertEqual(server._browser_inputs["input"]["latest_timestamp_ns"], 123)
