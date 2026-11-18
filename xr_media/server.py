"""One-origin WebRTC media and WebXR server."""

import asyncio
from contextlib import suppress
from dataclasses import asdict
import json
import logging
import math
import threading
import time
from typing import Callable, Dict, Optional, Set

from .media import MediaServer
from aiohttp import WSMsgType, web
from .paths import static_dir
from .xr import XrSample

log = logging.getLogger("xr_media.server")
OnXrSample = Callable[[XrSample], None]
ASSET_VERSION = "20261118-framerate2"
MAX_HAPTIC_MS = 5000.0
_XR_ERROR_LOG_PERIOD_S = 5.0
_XR_TOKEN_MAX = 128


def _frame_rate(value) -> Optional[float]:
    """A positive finite frame rate from the client, or None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value > 0 else None

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
        self._xr_tokens: Dict[web.WebSocketResponse, str] = {}
        self._xr_closing: Set[asyncio.Task] = set()
        self._xr_state = {
            "mode": None,
            "passthrough": False,
            "preview": False,
            "ui_locked": False,
            "frame_rate": None,
        }
        self._xr_received = 0
        self._xr_dispatched = 0
        self._xr_replaced = 0

    @property
    def latest_xr(self) -> Optional[XrSample]:
        """Most recent XR sample, or ``None`` before the first one; it is kept after tracking stops."""
        with self._xr_lock:
            return self._latest_xr

    def get_xr_stats(self) -> dict:
        """Return the counters ``xr_server_received``, ``xr_server_dispatched``, ``xr_server_replaced``
        and ``xr_frame_rate``, the headset's reported XR frame rate in Hz (``None`` when unknown)."""
        with self._xr_lock:
            frame_rate = self._xr_state["frame_rate"]
        return {
            "xr_server_received": self._xr_received,
            "xr_server_dispatched": self._xr_dispatched,
            "xr_server_replaced": self._xr_replaced,
            "xr_frame_rate": frame_rate,
        }

    def send_haptic(self, side: str, intensity: float, duration_ms: float) -> bool:
        """Pulse the ``"left"`` or ``"right"`` controller of the XR client; callable from any thread.

        ``intensity`` is clipped to [0, 1] and ``duration_ms`` to [0, 5000]. Returns False when no XR
        client is connected; delivery and device support are best effort.
        """
        if side not in ("left", "right"):
            raise ValueError("side must be 'left' or 'right'")
        intensity, duration_ms = float(intensity), float(duration_ms)
        if not (math.isfinite(intensity) and math.isfinite(duration_ms)):
            raise ValueError("intensity and duration_ms must be finite")
        message = {
            "op": "xr.haptic", "side": side,
            "intensity": min(max(intensity, 0.0), 1.0),
            "duration_ms": min(max(duration_ms, 0.0), MAX_HAPTIC_MS),
        }
        loop = self._loop
        if loop is None or not loop.is_running() or not self._xr_peers:
            return False
        asyncio.run_coroutine_threadsafe(self._send_xr(message), loop)
        return True

    async def _send_xr(self, message: dict) -> None:
        for ws in list(self._xr_peers):
            if ws.closed:
                continue
            with suppress(ConnectionError, RuntimeError):
                await ws.send_json(message)

    def configure_app(self, app: web.Application) -> None:
        """Add the XR signaling, state, client-report and asset routes."""
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
        # The page sends one random token per XR session. A reconnect with the same token replaces
        # its own previous socket, which may stay half-open until the heartbeat times out.
        token = request.query.get("session", "")
        if not 16 <= len(token) <= _XR_TOKEN_MAX:
            token = ""
        if self._xr_peers:
            if not token or any(self._xr_tokens.get(peer) != token for peer in self._xr_peers):
                await ws.send_json({
                    "op": "session.busy", "message": "XR is in use by another client; retry after it exits",
                })
                await ws.close(code=1008, message=b"XR occupied")
                return ws
            log.info("XR client reconnected; replacing its previous connection")
            for peer in list(self._xr_peers):
                self._xr_peers.discard(peer)
                # closing a half-open socket waits for its close timeout: do not delay the new one
                task = asyncio.create_task(peer.close(code=1000, message=b"XR reconnected"))
                self._xr_closing.add(task)
                task.add_done_callback(self._xr_closing.discard)
        self._xr_peers.add(ws)
        self._xr_tokens[ws] = token
        pending_samples = asyncio.Queue(maxsize=5)

        async def dispatch_all() -> None:
            last_error_log = None
            suppressed = 0
            while True:
                sample = await pending_samples.get()
                if self.on_xr is not None:
                    self._xr_dispatched += 1
                    try:
                        await asyncio.to_thread(self.on_xr, sample)
                    except Exception:
                        # One bad sample must not stop forwarding for the rest of the session.
                        # A handler failing on every sample would log at the XR rate, so throttle.
                        now = time.monotonic()
                        if last_error_log is not None and now - last_error_log < _XR_ERROR_LOG_PERIOD_S:
                            suppressed += 1
                            continue
                        log.exception(
                            "XR sample handler failed (%d similar failures suppressed)", suppressed,
                        )
                        last_error_log, suppressed = now, 0

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
                        "frame_rate": _frame_rate(body.get("frame_rate")),
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
            self._xr_tokens.pop(ws, None)
            if not self._xr_peers:
                with self._xr_lock:
                    self._xr_state = {
                        "mode": None,
                        "passthrough": False,
                        "preview": False,
                        "ui_locked": False,
                        "frame_rate": None,
                    }
        return ws

    async def stop(self) -> None:
        """Close XR WebSockets, then stop the media server."""
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
