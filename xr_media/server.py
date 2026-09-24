"""One-origin WebRTC media and WebXR server."""

import asyncio
from contextlib import suppress
from dataclasses import asdict
import json
import logging
import threading
from typing import Callable, Optional, Set

from .media import MediaServer
from aiohttp import WSMsgType, web
from .paths import static_dir
from .xr import XrSample

log = logging.getLogger("xr_media.server")
OnXrSample = Callable[[XrSample], None]
ASSET_VERSION = "20260923-admission"


class XrMediaServer(MediaServer):
    """Media server with WebXR input and an immersive card UI."""

    def __init__(
        self, *args, on_xr: Optional[OnXrSample] = None, **kwargs
    ):
        super().__init__(*args, **kwargs)
        self.on_xr = on_xr
        self._latest_xr: Optional[XrSample] = None
        self._xr_lock = threading.Lock()
        self._xr_peers: Set[web.WebSocketResponse] = set()
        self._xr_state = {
            "mode": None,
            "passthrough": False,
            "preview": False,
            "ui_locked": False,
        }
        self._xr_received = 0
        self._xr_dispatched = 0
        self._xr_replaced = 0

    @property
    def latest_xr(self) -> Optional[XrSample]:
        with self._xr_lock:
            return self._latest_xr

    def get_xr_stats(self) -> dict:
        return {
            "xr_server_received": self._xr_received,
            "xr_server_dispatched": self._xr_dispatched,
            "xr_server_replaced": self._xr_replaced,
        }

    def configure_app(self, app: web.Application) -> None:
        app.router.add_get("/xr-signal", self._xr_signal)
        app.router.add_get("/api/xr/state", self._xr_state_response)
        app.router.add_post("/api/xr/error", self._client_error)
        app.router.add_post("/api/xr/event", self._client_event)
        # Required for package data installed by colcon --symlink-install.
        app.router.add_static("/xr/", static_dir(), follow_symlinks=True)

    async def _client_error(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            body = {}
        log.error(
            "XR client error during %s: %s\n%s",
            body.get("phase", "unknown"),
            body.get("message", "unknown"),
            body.get("stack", ""),
        )
        return web.json_response({"ok": True})

    async def _client_event(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            body = {}
        log.info(
            "XR client phase=%s details=%s",
            body.get("phase", "unknown"),
            body.get("details", {}),
        )
        return web.json_response({"ok": True})

    async def _xr_state_response(self, request: web.Request) -> web.Response:
        del request
        sample = self.latest_xr
        return web.json_response({
            "xr": asdict(sample) if sample else None,
            "session": dict(self._xr_state),
        })

    async def _index(self, request: web.Request) -> web.Response:
        del request
        page = (static_dir() / "index.html").read_text(encoding="utf-8")
        page = page.replace(
            "</head>",
            '<link rel="stylesheet" href="/xr/xr.css?v=%s"></head>'
            % ASSET_VERSION,
            1,
        )
        page = page.replace(
            "</body>",
            '<canvas id="xr-stage" aria-hidden="true"></canvas>'
            '<script type="module" src="/xr/xr.js?v=%s"></script></body>'
            % ASSET_VERSION,
            1,
        )
        return web.Response(
            text=page, content_type="text/html",
            headers={"Cache-Control": "no-store"},
        )

    async def _xr_signal(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        if self._xr_peers:
            await ws.send_json({
                "op": "session.busy", "message": "XR 已被其他客户端占用，请等待对方退出后重试",
            })
            await ws.close(code=1008, message=b"XR occupied")
            return ws
        self._xr_peers.add(ws)
        pending_samples = asyncio.Queue(maxsize=5)

        async def dispatch_all() -> None:
            while True:
                sample = await pending_samples.get()
                if self.on_xr is not None:
                    self._xr_dispatched += 1
                    await asyncio.to_thread(self.on_xr, sample)

        dispatcher = asyncio.create_task(dispatch_all())
        try:
            await ws.send_json({"op": "session.config"})
            async for message in ws:
                if message.type != WSMsgType.TEXT:
                    continue
                try:
                    body = json.loads(message.data)
                    if body.get("op") == "client.error":
                        log.error(
                            "XR client error: %s\n%s",
                            body.get("message", "unknown"),
                            body.get("stack", ""),
                        )
                        continue
                    if body.get("op") != "xr.sample":
                        continue
                    sample = XrSample.from_json(body)
                except (json.JSONDecodeError, TypeError, ValueError):
                    log.warning("invalid XR sample")
                    continue
                with self._xr_lock:
                    self._latest_xr = sample
                    self._xr_state = {
                        "mode": body.get("xr_mode"),
                        "passthrough": bool(body.get("passthrough")),
                        "preview": bool(body.get("preview")),
                        "ui_locked": bool(body.get("ui_locked")),
                    }
                self._xr_received += 1
                if pending_samples.full():
                    pending_samples.get_nowait()
                    self._xr_replaced += 1
                pending_samples.put_nowait(sample)
        finally:
            dispatcher.cancel()
            with suppress(asyncio.CancelledError):
                await dispatcher
            self._xr_peers.discard(ws)
            if not self._xr_peers:
                with self._xr_lock:
                    self._xr_state = {
                        "mode": None,
                        "passthrough": False,
                        "preview": False,
                        "ui_locked": False,
                    }
        return ws

    async def stop(self) -> None:
        peers = list(self._xr_peers)
        self._xr_peers.clear()
        if peers:
            try:
                await asyncio.wait_for(
                    asyncio.gather(
                        *(ws.close(code=1001, message=b"server shutdown") for ws in peers),
                        return_exceptions=True,
                    ),
                    timeout=0.5,
                )
            except asyncio.TimeoutError:
                log.warning("timed out closing XR WebSockets")
        await super().stop()
