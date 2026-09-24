import asyncio
import socket
from unittest.mock import AsyncMock, Mock, patch

import pytest

from xr_media import MediaServer, MediaServerConfig


@pytest.mark.parametrize("failure", ["setup", "tls", "bind", "cancel"])
def test_failed_start_cleans_runner_and_can_retry(failure):
    async def exercise():
        server = MediaServer()
        runner = Mock(setup=AsyncMock(), cleanup=AsyncMock())
        site = Mock(start=AsyncMock())
        error = asyncio.CancelledError() if failure == "cancel" else OSError("startup failed")
        if failure == "setup":
            runner.setup.side_effect = error
        elif failure in {"bind", "cancel"}:
            site.start.side_effect = error
        with patch("xr_media.media.server.web.AppRunner", return_value=runner), patch(
            "xr_media.media.server.web.TCPSite", return_value=site
        ), patch.object(server, "_ssl_context") as tls:
            if failure == "tls":
                tls.side_effect = error
            with pytest.raises(type(error)):
                await server.start()
            assert server._runner is None
            runner.cleanup.assert_awaited_once()
            runner.setup.side_effect = None
            site.start.side_effect = None
            tls.side_effect = None
            site.start.reset_mock()
            try:
                await server.start()
                site.start.assert_awaited_once()
                assert server._runner is runner
            finally:
                await server.stop()
    asyncio.run(exercise())


def test_cleanup_failure_does_not_hide_startup_error():
    async def exercise():
        server = MediaServer()
        runner = Mock(
            setup=AsyncMock(side_effect=OSError("original failure")),
            cleanup=AsyncMock(side_effect=RuntimeError("cleanup failed")),
        )
        with patch("xr_media.media.server.web.AppRunner", return_value=runner):
            with pytest.raises(OSError, match="original failure"):
                await server.start()
            assert server._runner is None
    asyncio.run(exercise())


@pytest.mark.parametrize("background", [False, True])
def test_retry_after_port_becomes_available(background, tmp_path):
    occupied = socket.socket()
    occupied.bind(("127.0.0.1", 0))
    occupied.listen()
    port = occupied.getsockname()[1]
    server = MediaServer(MediaServerConfig(
        host="127.0.0.1", port=port,
        cert_path=tmp_path / "missing-cert.pem", key_path=tmp_path / "missing-key.pem",
    ))

    async def check_listener():
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(b"GET /api/streams HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), 3)
            assert response.startswith(b"HTTP/1.1 200")
        finally:
            writer.close()
            await writer.wait_closed()

    async def exercise():
        try:
            with pytest.raises(OSError):
                await server.start()
            assert server._runner is None
            occupied.close()
            await server.start()
            await check_listener()
        finally:
            await server.stop()

    try:
        if background:
            with pytest.raises(RuntimeError, match="failed to start"):
                server.start_background()
            assert server._runner is None
            assert not server._thread.is_alive()
            assert server._loop.is_closed()
            occupied.close()
            try:
                server.start_background()
                asyncio.run(check_listener())
            finally:
                server.stop_background()
        else:
            asyncio.run(exercise())
    finally:
        occupied.close()
