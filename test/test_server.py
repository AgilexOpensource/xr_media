import asyncio
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from xr_media import HandInput, XrMediaServer
from xr_media.paths import static_dir, tls_dir


class XrMediaServerTest(unittest.TestCase):
    def test_xr_session_is_exclusive_and_released_on_disconnect(self):
        async def scenario():
            server = XrMediaServer()
            app = web.Application()
            app.router.add_get('/xr-signal', server._xr_signal)
            async with TestClient(TestServer(app)) as client:
                first = await client.ws_connect('/xr-signal')
                self.assertEqual((await first.receive_json(timeout=2))['op'], 'session.config')
                second = await client.ws_connect('/xr-signal')
                self.assertEqual((await second.receive_json(timeout=2))['op'], 'session.busy')
                await second.close()
                self.assertEqual(len(server._xr_peers), 1)
                self.assertFalse(first.closed)
                await first.close()
                async def wait_for_release():
                    while server._xr_peers:
                        await asyncio.sleep(0.01)
                await asyncio.wait_for(wait_for_release(), 2)
                third = await client.ws_connect('/xr-signal')
                self.assertEqual((await third.receive_json(timeout=2))['op'], 'session.config')
                await third.close()
        asyncio.run(scenario())

    def test_xr_websocket_preserves_fifo_at_capacity(self):
        self._check_busy_callback_queue(6, [1, 2, 3, 4, 5, 6], 0)

    def test_xr_websocket_drops_oldest_when_full(self):
        self._check_busy_callback_queue(9, [1, 5, 6, 7, 8, 9], 3)

    def _check_busy_callback_queue(self, last_timestamp, expected, dropped):
        received = []
        callback_started = threading.Event()
        release_callback = threading.Event()

        def on_xr(sample):
            received.append(sample.t_ms)
            if sample.t_ms == 1:
                callback_started.set()
                release_callback.wait(timeout=10.0)

        async def scenario():
            server = XrMediaServer(on_xr=on_xr)
            app = web.Application()
            app.router.add_get("/xr-signal", server._xr_signal)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                ws = await client.ws_connect("/xr-signal")
                self.assertEqual((await ws.receive_json())["op"], "session.config")

                await ws.send_json({"op": "xr.sample", "t_ms": 1})
                started = await asyncio.to_thread(callback_started.wait, 1.0)
                self.assertTrue(started)

                for timestamp in range(2, last_timestamp + 1):
                    await ws.send_json({"op": "xr.sample", "t_ms": timestamp})
                for _ in range(200):
                    if server.get_xr_stats()["xr_server_received"] == last_timestamp:
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(server.get_xr_stats()["xr_server_received"], last_timestamp)
                self.assertEqual(server.get_xr_stats()["xr_server_replaced"], dropped)
                self.assertEqual(received, [1])
                release_callback.set()
                for _ in range(100):
                    if received == expected:
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(received, expected)
                self.assertEqual(server.get_xr_stats()["xr_server_replaced"], dropped)
                await ws.close()
            finally:
                release_callback.set()
                await client.close()

        asyncio.run(scenario())

    def test_tls_dir_uses_xdg_config_home(self):
        with tempfile.TemporaryDirectory() as root:
            with unittest.mock.patch.dict(
                os.environ, {"XDG_CONFIG_HOME": root}
            ):
                self.assertEqual(tls_dir(), Path(root) / "xr_media" / "tls")

    def test_static_dir_finds_pip_installed_assets_without_ros(self):
        with tempfile.TemporaryDirectory() as root:
            prefix = Path(root)
            assets = prefix / "share" / "xr_media" / "static"
            assets.mkdir(parents=True)
            with (
                patch.dict("sys.modules", {"ament_index_python.packages": None}),
                patch("xr_media.paths.__file__", str(prefix / "lib/xr_media/paths.py")),
                patch("xr_media.paths.sys.prefix", root),
            ):
                self.assertEqual(static_dir(), assets)

    def test_trigger_boolean_is_not_inferred_from_analog_value(self):
        released = HandInput.from_mapping(
            {"trigger_pressed": False, "trigger_analog": 0.1}
        )
        self.assertFalse(released.trigger_pressed)
        self.assertEqual(released.trigger_analog, 0.1)

    def test_stop_closes_xr_websockets(self):
        server = XrMediaServer()
        socket = MagicMock()
        socket.close = AsyncMock()
        server._xr_peers.add(socket)

        asyncio.run(server.stop())

        socket.close.assert_awaited_once_with(
            code=1001, message=b"server shutdown"
        )
        self.assertFalse(server._xr_peers)

    def test_assets_and_xr_state(self):
        server = XrMediaServer()
        self.assertTrue((static_dir() / "xr.js").is_file())
        self.assertTrue((static_dir() / "vr-ui.js").is_file())
        self.assertTrue((static_dir() / "vendor" / "three.module.min.js").is_file())
        self.assertTrue((static_dir() / "vendor" / "three.core.min.js").is_file())
        self.assertIsNone(server.latest_xr)
        self.assertFalse(server._xr_state["ui_locked"])

    def test_index_includes_xr_card(self):
        response = asyncio.run(XrMediaServer()._index(None))
        self.assertIn('/xr/xr.css?v=', response.text)
        self.assertIn('id="xr-stage"', response.text)
        self.assertIn('/xr/xr.js?v=', response.text)


if __name__ == "__main__":
    unittest.main()
