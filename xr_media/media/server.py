"""HTTPS signaling server and WebRTC peer lifecycle."""

import asyncio
import concurrent.futures
import html
import json
import logging
import socket
import ssl
import shutil
import subprocess
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from ..dependencies import require_dependencies

require_dependencies(
    ("numpy", "numpy"), ("aiohttp", "aiohttp"), ("av", "av"), ("aiortc", "aiortc")
)

from aiohttp import WSMsgType, web
import av
from aiortc import (
    RTCBundlePolicy,
    RTCConfiguration,
    RTCIceCandidate,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
)
from aiortc.sdp import SessionDescription

from ..paths import static_dir, tls_dir
from .audio import AudioModule
from .frame import AudioFrame, MediaServerConfig, MediaServerStats, VideoFrame
from .media import MediaModule, PeerMediaContext
from .video import VideoModule

log = logging.getLogger("xr_media.media.server")


def _defer_mdns_candidates(
    sdp: str,
) -> Tuple[str, Optional[Tuple[List[RTCIceCandidate], bool]]]:
    description = SessionDescription.parse(sdp)
    if not any(candidate.ip.lower().endswith(".local")
               for media in description.media for candidate in media.ice_candidates):
        return sdp, None
    candidates = []
    for index, media in enumerate(description.media):
        for candidate in media.ice_candidates:
            candidate.sdpMid = media.rtp.muxId
            candidate.sdpMLineIndex = index
            candidates.append(candidate)
    stripped = "".join(line for line in sdp.splitlines(keepends=True)
                       if not line.startswith(("a=candidate:", "a=end-of-candidates")))
    complete = all(media.ice_candidates_complete for media in description.media)
    return stripped, (candidates, complete)


def lan_ipv4_addresses() -> List[str]:
    found: List[str] = []
    try:
        output = subprocess.check_output(
            ["hostname", "-I"], stderr=subprocess.DEVNULL, text=True
        )
        found.extend(
            ip
            for ip in output.split()
            if ip.count(".") == 3 and not ip.startswith("127.")
        )
    except (OSError, subprocess.CalledProcessError):
        pass
    if not found:
        try:
            ip = socket.gethostbyname(socket.gethostname())
            if not ip.startswith("127."):
                found.append(ip)
        except OSError:
            pass
    return list(dict.fromkeys(found))


class MediaServer:
    """Exchange video and audio with browser WebRTC peers."""

    def __init__(self, config: Optional[MediaServerConfig] = None) -> None:
        self.config = config or MediaServerConfig()
        self._peers: Set[RTCPeerConnection] = set()
        self._runner: Optional[web.AppRunner] = None
        self._sent = 0
        self._sent_lock = threading.Lock()
        self.video = VideoModule(self.config, self._mark_sent)
        self.audio = AudioModule()
        self.modules: List[MediaModule] = [self.video, self.audio]
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._started = threading.Event()
        self._start_error: Optional[BaseException] = None
        self._browser_inputs: Dict[str, dict] = {}
        self._input_frames: Dict[str, Any] = {}
        self._input_preview_cache: Dict[str, tuple] = {}
        self._input_preview_locks: Dict[str, asyncio.Lock] = {}
        self._input_audio_samples: Dict[str, list] = {}
        self._audio_players: Dict[str, subprocess.Popen] = {}
        self._audio_player_errors: Dict[str, str] = {}
        self._input_owners: Dict[str, web.WebSocketResponse] = {}
        self._peer_signals: Dict[RTCPeerConnection, web.WebSocketResponse] = {}
        self._signal_peers: Set[web.WebSocketResponse] = set()
        self._clients: Dict[web.WebSocketResponse, dict] = {}
        self._client_sequence = 0
        self._logged_input_audio: Set[str] = set()

    def add_audio_listener(
        self, callback: Callable[[str, AudioFrame], Any]
    ) -> None:
        """Register ``callback(stream_id, frame)`` for browser PCM blocks."""
        self.audio.add_listener(callback)

    def remove_audio_listener(
        self, callback: Callable[[str, AudioFrame], Any]
    ) -> None:
        self.audio.remove_listener(callback)

    def add_video_listener(
        self, callback: Callable[[str, VideoFrame], Any]
    ) -> None:
        """Register ``callback(stream_id, frame)`` for browser video frames."""
        self.video.add_listener(callback)

    def remove_video_listener(
        self, callback: Callable[[str, VideoFrame], Any]
    ) -> None:
        self.video.remove_listener(callback)

    def register_browser_input(
        self,
        stream_id: str,
        media_type: str,
        label: str = "",
        on_control: Optional[Callable[[str, Any], None]] = None,
    ) -> None:
        """Expose an opt-in browser device/file input card."""
        if media_type not in {"video", "audio"}:
            raise ValueError("media_type must be video or audio")
        valid = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
        if not stream_id or any(char not in valid for char in stream_id):
            raise ValueError("stream_id must contain only letters, numbers, '_' or '-'")
        self._browser_inputs[stream_id] = {
            "id": stream_id,
            "label": label or stream_id,
            "media_type": media_type,
            "direction": "browser_to_pc",
            "kind": "%s_input" % media_type,
            "status": "idle",
            "preview": False,
            "speaker": False,
            "frames_received": 0,
            "latest_timestamp_ns": 0,
            "control": on_control,
        }

    def register_audio_stream(
        self, stream_id: str, label: str = "", kind: str = "audio"
    ) -> None:
        self.audio.register_audio_stream(stream_id, label, kind)

    def push_audio_frame(
        self, frame: AudioFrame, stream_id: str = "audio", label: str = ""
    ) -> None:
        """Publish a 16 kHz mono int16 PCM block to every browser peer."""
        self.audio.push(frame, stream_id, label)

    @property
    def page_urls(self) -> List[str]:
        hosts = lan_ipv4_addresses() or ["localhost"]
        scheme = "https" if self._tls_paths() else "http"
        return [f"{scheme}://{host}:{self.config.port}/" for host in hosts]

    def register_video_stream(
        self,
        stream_id: str,
        label: str = "",
        kind: str = "",
        on_control: Optional[Callable[[str, Any], None]] = None,
    ) -> None:
        self.video.register_video_stream(
            stream_id, label, kind, on_control
        )

    def push_video_frame(
        self, frame: VideoFrame, stream_id: str = "main", label: str = ""
    ) -> None:
        self.video.push_video_frame(frame, stream_id, label)

    def set_stream_status(self, stream_id: str, status: str) -> None:
        self.video.set_stream_status(stream_id, status)

    def set_stream_playback(self, stream_id: str, **values) -> None:
        self.video.set_stream_playback(stream_id, **values)

    def list_streams(self) -> List[dict]:
        return self.video.list_streams()

    def get_stats(self) -> MediaServerStats:
        states, counts, snapshots = self.video.counts()
        received = sum(item[0] for item in counts)
        replaced = sum(item[1] for item in counts)
        with self._sent_lock:
            sent = self._sent
        return MediaServerStats(
            clients=len(self._peers),
            frames_received=received,
            frames_replaced=replaced,
            frames_sent=sent,
            latest_timestamp_ns=max(
                (frame.timestamp_ns for frame in snapshots if frame), default=0
            ),
            streams=len(states),
            audio_frames_received=self.audio.frames_received,
            audio_frames_sent=self.audio.frames_sent,
        )

    def _mark_sent(self) -> None:
        with self._sent_lock:
            self._sent += 1

    def _tls_paths(self):
        cert = (
            Path(self.config.cert_path)
            if self.config.cert_path
            else tls_dir() / "cert.pem"
        )
        key = (
            Path(self.config.key_path)
            if self.config.key_path
            else tls_dir() / "key.pem"
        )
        return (cert, key) if cert.is_file() and key.is_file() else None

    def asset_root(self) -> Path:
        """Static asset directory. Integration servers may override this."""
        return static_dir()

    def configure_app(self, app: web.Application) -> None:
        """Register optional integration routes before the catch-all assets."""
        del app

    def _ssl_context(self) -> Optional[ssl.SSLContext]:
        paths = self._tls_paths()
        if paths is None:
            log.warning("TLS certificate not found; serving HTTP for local development")
            return None
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(paths[0]), str(paths[1]))
        return context

    def _ice_configuration(self) -> RTCConfiguration:
        servers = []
        for url, username, credential in self.config.ice_servers:
            servers.append(
                RTCIceServer(urls=url, username=username, credential=credential)
            )
        return RTCConfiguration(iceServers=servers, bundlePolicy=RTCBundlePolicy.MAX_BUNDLE)

    async def _new_peer(self, stream_ids: List[str], browser_inputs=None):
        pc = RTCPeerConnection(self._ice_configuration())
        self._peers.add(pc)
        context = PeerMediaContext(stream_ids)
        context.values["browser.inputs"] = browser_inputs or []
        context.values["browser.video_frame"] = lambda stream_id, frame: (
            self._peer_signals.get(pc) is not None
            and self._input_owners.get(stream_id) is self._peer_signals.get(pc)
            and self._store_input_video_frame(stream_id, frame)
        )
        context.values["browser.audio_frame"] = lambda stream_id, frame: (
            self._peer_signals.get(pc) is not None
            and self._input_owners.get(stream_id) is self._peer_signals.get(pc)
            and self._play_input_audio_frame(stream_id, frame)
        )
        for module in self.modules:
            module.attach(pc, context)

        @pc.on("connectionstatechange")
        async def on_connectionstatechange() -> None:
            log.info("peer state=%s clients=%d", pc.connectionState, len(self._peers))
            # "disconnected" is commonly transient on mobile Wi-Fi and can
            # recover or be followed by renegotiation on the same peer.
            if pc.connectionState in {"failed", "closed"}:
                await asyncio.gather(
                    *(module.detach(pc) for module in self.modules),
                    return_exceptions=True,
                )
                if pc.connectionState != "closed":
                    await pc.close()
                self._peers.discard(pc)
                signal = self._peer_signals.pop(pc, None)
                if signal is not None and not signal.closed:
                    await signal.close()

        return pc, context

    async def _websocket(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        # Reserve a seat before accepting controls or starting media negotiation.
        # There is no await between the capacity check and reservation.
        if len(self._clients) >= self.config.max_clients:
            clients = list(self._clients.values())
            await ws.send_json({
                "type": "error", "code": "capacity_full",
                "count": len(clients), "limit": self.config.max_clients,
                "clients": clients,
            })
            await ws.close(code=1013, message=b"server full")
            return ws
        self._client_sequence += 1
        agent = request.headers.get("User-Agent", "")
        browser = next((name for token, name in (
            ("Edg/", "Edge"), ("Firefox/", "Firefox"),
            ("Chrome/", "Chrome"), ("Safari/", "Safari"),
        ) if token in agent), "Browser")
        self._clients[ws] = {
            "id": f"client-{self._client_sequence}", "browser": browser,
        }
        self._signal_peers.add(ws)
        pc: Optional[RTCPeerConnection] = None
        context: Optional[PeerMediaContext] = None
        tracks = {}
        offer_sequence = 0
        pending_ice = None
        try:
            if request.query.get("admission") == "1":
                await ws.send_json({"type": "session.accepted"})
            async for message in ws:
                if message.type != WSMsgType.TEXT:
                    continue
                body = json.loads(message.data)
                if body.get("type") == "answer.applied":
                    if (pc is not None and pending_ice is not None
                            and body.get("offer_id") == offer_sequence):
                        candidates, complete = pending_ice
                        pending_ice = None
                        for candidate in candidates:
                            await pc.addIceCandidate(candidate)
                        if complete:
                            await pc.addIceCandidate(None)
                        log.info("processed deferred ICE candidates after answer: offer=%s count=%s",
                                 offer_sequence, len(candidates))
                    continue
                if body.get("type") == "stream.control":
                    stream_id = str(body.get("stream", ""))
                    with self.video.lock:
                        state = self.video.streams.get(stream_id)
                        callback = state.control if state is not None else None
                        if state is not None and body.get("action") == "seek":
                            state.status = "seeking"
                    if callback is not None:
                        await asyncio.to_thread(
                            callback,
                            str(body.get("action", "")),
                            body.get("value"),
                        )
                    continue
                if body.get("type") == "input.control":
                    input_state = self._browser_inputs.get(str(body.get("stream", "")))
                    if input_state is not None:
                        action = str(body.get("action", ""))
                        stream_id = input_state["id"]
                        owner = self._input_owners.get(stream_id)
                        if owner is not None and owner is not ws:
                            await ws.send_json({
                                "type": "input.result", "stream": stream_id,
                                "action": action, "ok": False,
                                "code": "input_busy",
                                "client": self._clients.get(owner, {}),
                            })
                            continue
                        if action == "claim":
                            self._input_owners[stream_id] = ws
                            await ws.send_json({
                                "type": "input.result", "stream": stream_id,
                                "action": action, "ok": True,
                            })
                            continue
                        if owner is None and action != "start":
                            continue
                        if action == "start":
                            self._input_owners[stream_id] = ws
                        new_status = str(body.get("status", input_state["status"]))
                        if (
                            action in {"preview", "speaker"}
                            and body.get("value") is True
                            and input_state["status"] not in {"live", "paused"}
                        ):
                            await ws.send_json({
                                "type": "input.result", "stream": input_state["id"],
                                "action": action, "ok": False,
                            })
                            continue
                        input_state["status"] = new_status
                        if action in {"preview", "speaker"}:
                            input_state[action] = body.get("value") is True
                        callback = input_state.get("control")
                        if callback is not None:
                            await asyncio.to_thread(
                                callback, action, body.get("value")
                            )
                        if body.get("action") == "preview" and body.get("value") is True:
                            scheme = "https" if self._tls_paths() else "http"
                            url = "%s://localhost:%d/input-preview/%s" % (
                                scheme, self.config.port, input_state["id"]
                            )
                            opened = await asyncio.to_thread(webbrowser.open, url)
                            await ws.send_json({
                                "type": "input.result", "stream": input_state["id"],
                                "action": "preview", "ok": bool(opened), "url": url,
                            })
                        if input_state["media_type"] == "audio" and body.get("action") == "speaker":
                            enabled = body.get("value") is True
                            ok = self._set_input_speaker(input_state["id"], enabled)
                            await ws.send_json({
                                "type": "input.result", "stream": input_state["id"],
                                "action": "speaker", "ok": ok,
                                "error": self._audio_player_errors.get(input_state["id"], ""),
                            })
                        if body.get("action") in {"stop", "ended"}:
                            if self._input_owners.get(input_state["id"]) is ws:
                                self._input_owners.pop(input_state["id"], None)
                            input_state["preview"] = False
                            input_state["speaker"] = False
                            self._input_frames.pop(input_state["id"], None)
                            self._input_preview_cache.pop(input_state["id"], None)
                            self._input_audio_samples.pop(input_state["id"], None)
                            input_state["frames_received"] = 0
                            input_state["latest_timestamp_ns"] = 0
                            if input_state["media_type"] == "audio":
                                self._set_input_speaker(input_state["id"], False)
                    continue
                if body.get("type") in {"stream.pause", "stream.resume"}:
                    track = tracks.get(str(body.get("stream", "")))
                    if track is not None:
                        track.pause() if body["type"] == "stream.pause" else track.resume()
                    continue
                if body.get("type") != "offer":
                    continue
                offer_sequence += 1
                remote_sdp = body["sdp"]
                pending_ice = None
                if body.get("defer_mdns") is True:
                    remote_sdp, pending_ice = _defer_mdns_candidates(remote_sdp)
                requested = body.get("streams")
                if not isinstance(requested, list):
                    requested = [item["id"] for item in self.list_streams()]
                browser_inputs = body.get("inputs")
                if not isinstance(browser_inputs, list):
                    browser_inputs = []
                if pc is None:
                    pc, context = await self._new_peer(
                        [str(item) for item in requested], browser_inputs
                    )
                    self._peer_signals[pc] = ws
                assert context is not None
                context.values["browser.inputs"] = browser_inputs
                selected = context.values["video.selected"]
                tracks = {
                    **context.values["video.tracks"],
                    **context.values["audio.tracks"],
                }
                await pc.setRemoteDescription(
                    RTCSessionDescription(sdp=remote_sdp, type="offer")
                )
                answer = await pc.createAnswer()
                await pc.setLocalDescription(answer)
                video_transceivers = [
                    item for item in pc.getTransceivers() if item.kind == "video"
                ]
                audio_transceivers = [
                    item for item in pc.getTransceivers() if item.kind == "audio"
                ]
                stream_map = [
                    {
                        "mid": transceiver.mid,
                        "id": stream.stream_id,
                        "label": stream.label,
                        "media_type": "video",
                    }
                    for transceiver, stream in zip(video_transceivers, selected)
                ]
                stream_map.extend(
                    {
                        "mid": transceiver.mid,
                        "id": stream.stream_id,
                        "label": stream.label,
                        "media_type": "audio",
                    }
                    for transceiver, stream in zip(
                        audio_transceivers, context.values["audio.selected"]
                    )
                )
                await ws.send_json({
                    "type": pc.localDescription.type,
                    "sdp": pc.localDescription.sdp,
                    "streams": stream_map,
                    "ice_deferred": offer_sequence if pending_ice is not None else None,
                })
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            log.warning("invalid signaling message: %s", exc)
        finally:
            self._clients.pop(ws, None)
            self._signal_peers.discard(ws)
            for stream_id, owner in list(self._input_owners.items()):
                if owner is not ws:
                    continue
                self._input_owners.pop(stream_id, None)
                input_state = self._browser_inputs.get(stream_id)
                if input_state is None:
                    continue
                input_state["status"] = "idle"
                input_state["preview"] = False
                input_state["speaker"] = False
                self._input_frames.pop(stream_id, None)
                self._input_audio_samples.pop(stream_id, None)
                input_state["frames_received"] = 0
                input_state["latest_timestamp_ns"] = 0
                if input_state["media_type"] == "audio":
                    self._set_input_speaker(stream_id, False)
            if pc is not None:
                await asyncio.gather(
                    *(module.detach(pc) for module in self.modules),
                    return_exceptions=True,
                )
                await pc.close()
                self._peers.discard(pc)
                self._peer_signals.pop(pc, None)
        return ws

    async def _stats(self, request: web.Request) -> web.Response:
        del request
        return web.json_response(self.get_stats().__dict__)

    async def _stream_list(self, request: web.Request) -> web.Response:
        del request
        return web.json_response({
            "streams": self.list_streams() + self.audio.list_streams() + [
                {key: value for key, value in item.items() if key != "control"}
                for item in self._browser_inputs.values()
            ],
        })

    def _store_input_video_frame(self, stream_id: str, frame: VideoFrame) -> bool:
        state = self._browser_inputs.get(stream_id)
        if state is None or state.get("status") != "live":
            return False
        state["frames_received"] = state.get("frames_received", 0) + 1
        state["latest_timestamp_ns"] = frame.timestamp_ns
        # Keep only the latest decoded frame. JPEG compression is deliberately
        # deferred to the HTTP request worker so it cannot stall WebRTC recv().
        if state.get("preview"):
            self._input_frames[stream_id] = frame.data
        return True

    @staticmethod
    def _encode_input_preview(data) -> bytes:
        """Encode one latest-frame JPEG without blocking the WebRTC loop."""
        height, width = data.shape[:2]
        codec = av.CodecContext.create("mjpeg", "w")
        codec.width = width
        codec.height = height
        codec.pix_fmt = "yuvj420p"
        frame = av.VideoFrame.from_ndarray(data, format="bgr24")
        return b"".join(bytes(packet) for packet in codec.encode(frame))

    async def _input_preview_jpeg(self, stream_id: str, data) -> bytes:
        lock = self._input_preview_locks.setdefault(stream_id, asyncio.Lock())
        async with lock:
            cached = self._input_preview_cache.get(stream_id)
            if cached is not None and cached[0] is data:
                return cached[1]
            encoded = await asyncio.to_thread(self._encode_input_preview, data)
            self._input_preview_cache[stream_id] = (data, encoded)
            return encoded

    def _set_input_speaker(self, stream_id: str, enabled: bool) -> bool:
        self._audio_player_errors.pop(stream_id, None)
        current = self._audio_players.pop(stream_id, None)
        if current is not None:
            try:
                if current.stdin:
                    current.stdin.close()
                current.terminate()
            except OSError:
                pass
        if not enabled:
            return True
        pulse = shutil.which("paplay")
        alsa = shutil.which("aplay")
        commands = []
        if pulse:
            commands.append([pulse, "--raw", "--rate=16000", "--channels=1", "--format=s16le"])
        if alsa:
            commands.append([alsa, "-q", "-t", "raw", "-f", "S16_LE", "-r", "16000", "-c", "1"])
        if not commands:
            error = (
                "PC Out requires paplay or aplay. On Ubuntu/Debian, install with: "
                "sudo apt install pulseaudio-utils (paplay) or "
                "sudo apt install alsa-utils (aplay)"
            )
            self._audio_player_errors[stream_id] = error
            log.error(error)
            return False
        errors = []
        for command in commands:
            try:
                player = subprocess.Popen(
                    command, stdin=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                time.sleep(0.08)
                if player.poll() is None:
                    self._audio_players[stream_id] = player
                    return True
                stderr = player.communicate()[1].decode("utf-8", "replace").strip()
                errors.append("%s: %s" % (Path(command[0]).name, stderr or "exited during startup"))
            except OSError as exc:
                errors.append("%s: %s" % (Path(command[0]).name, exc))
        error = "; ".join(errors)
        self._audio_player_errors[stream_id] = error
        log.error("PC Out unavailable: %s", error)
        return False

    def _play_input_audio_frame(self, stream_id: str, frame: AudioFrame) -> bool:
        state = self._browser_inputs.get(stream_id)
        if state is None or state.get("status") != "live":
            return False
        state["frames_received"] = state.get("frames_received", 0) + 1
        state["latest_timestamp_ns"] = frame.timestamp_ns
        if stream_id not in self._logged_input_audio:
            self._logged_input_audio.add(stream_id)
            log.info(
                "browser audio received stream=%s rate=%d channels=%d samples=%d",
                stream_id, frame.sample_rate, frame.channels, frame.samples,
            )
        samples = (
            frame.data.reshape(-1).tolist()
            if hasattr(frame.data, "reshape") else list(frame.data)
        )
        step = max(1, len(samples) // 512)
        self._input_audio_samples[stream_id] = samples[::step][:512]
        player = self._audio_players.get(stream_id)
        if player is None or player.stdin is None:
            return True
        if player.poll() is not None:
            self._set_input_speaker(stream_id, False)
            self._audio_player_errors[stream_id] = "audio output process exited"
            return True
        try:
            player.stdin.write(frame.data.tobytes())
            player.stdin.flush()
        except (BrokenPipeError, OSError):
            self._set_input_speaker(stream_id, False)
        return True

    async def _input_frame(self, request: web.Request) -> web.Response:
        stream_id = request.match_info["stream_id"]
        state = self._browser_inputs.get(stream_id)
        if state is None or not state.get("preview"):
            raise web.HTTPNoContent()
        data = self._input_frames.get(stream_id)
        if data is None:
            raise web.HTTPNoContent()
        frame = await self._input_preview_jpeg(stream_id, data)
        return web.Response(
            body=frame,
            content_type="image/jpeg",
            headers={"Cache-Control": "no-store"},
        )

    async def _input_audio(self, request: web.Request) -> web.Response:
        stream_id = request.match_info["stream_id"]
        state = self._browser_inputs.get(stream_id)
        samples = (
            self._input_audio_samples.get(stream_id, [])
            if state is not None and state.get("preview") else []
        )
        return web.json_response({
            "samples": samples,
            "frames_received": state.get("frames_received", 0) if state else 0,
            "latest_timestamp_ns": state.get("latest_timestamp_ns", 0) if state else 0,
        })

    async def _input_preview(self, request: web.Request) -> web.Response:
        stream_id = request.match_info["stream_id"]
        state = self._browser_inputs.get(stream_id)
        if state is None:
            raise web.HTTPNotFound()
        if state["media_type"] == "audio":
            page = """<!doctype html>
<meta charset=utf-8>
<title>{label}</title>
<style>
html, body {{
  margin: 0; width: 100%; height: 100%; background: #020405; overflow: hidden;
}}
canvas {{ width: 100%; height: 100%; }}
output {{
  position: fixed; left: 12px; top: 10px; color: #9db5b0; font: 12px monospace;
}}
</style>
<canvas id=c></canvas><output id=s>waiting for PC audio</output>
<script>
const c = document.querySelector('#c');
const x = c.getContext('2d');
const o = document.querySelector('#s');
async function draw() {{
  c.width = innerWidth * devicePixelRatio;
  c.height = innerHeight * devicePixelRatio;
  const p = await (await fetch(
    '/api/input-audio/{stream}?t=' + Date.now(), {{cache: 'no-store'}}
  )).json();
  const s = p.samples;
  x.fillStyle = '#020405';
  x.fillRect(0, 0, c.width, c.height);
  x.strokeStyle = '#58d6bd';
  x.lineWidth = 2 * devicePixelRatio;
  x.beginPath();
  s.forEach((v, i) => {{
    const px = i * c.width / Math.max(1, s.length - 1);
    const py = c.height / 2 - v / 32768 * c.height * .44;
    if (i) {{
      x.lineTo(px, py);
    }} else {{
      x.moveTo(px, py);
    }}
  }});
  x.stroke();
  const peak = s.reduce((m, v) => Math.max(m, Math.abs(v)), 0);
  o.textContent = p.frames_received
    ? 'frames ' + p.frames_received + ' · peak ' + peak
    : 'waiting for PC audio';
}}
setInterval(() => draw().catch(e => o.textContent = e.message), 50);
</script>""".format(
                label=html.escape(state["label"]), stream=stream_id
            )
            return web.Response(text=page, content_type="text/html")
        page = """<!doctype html>
<meta charset=utf-8>
<title>{label}</title>
<style>
html, body {{
  margin: 0; width: 100%; height: 100%; background: #020405; overflow: hidden;
}}
img {{ width: 100%; height: 100%; object-fit: contain; }}
</style>
<img id=f alt="{label}">
<script>
const f = document.querySelector('#f');
function next() {{
  setTimeout(() => f.src = '/api/input-frame/{stream}?t=' + Date.now(), 20);
}}
f.onload = next;
f.onerror = next;
next();
</script>""".format(
            label=html.escape(state["label"]), stream=stream_id
        )
        return web.Response(text=page, content_type="text/html")

    async def _index(self, request: web.Request) -> web.FileResponse:
        del request
        return web.FileResponse(self.asset_root() / "index.html")

    async def start(self) -> None:
        if self._runner is not None:
            return
        app = web.Application()
        assets = self.asset_root()
        app.router.add_get("/signal", self._websocket)
        app.router.add_get("/api/stats", self._stats)
        app.router.add_get("/api/streams", self._stream_list)
        app.router.add_get("/api/input-frame/{stream_id}", self._input_frame)
        app.router.add_get("/api/input-audio/{stream_id}", self._input_audio)
        app.router.add_get("/input-preview/{stream_id}", self._input_preview)
        self.configure_app(app)
        app.router.add_get("/", self._index)
        # colcon --symlink-install places package data here as symlinks into the
        # build tree.  aiohttp rejects those files unless link traversal is
        # explicitly enabled, which otherwise leaves the UI without CSS/JS.
        app.router.add_static("/static/", assets, follow_symlinks=True)
        self._runner = web.AppRunner(app, shutdown_timeout=1.0)
        try:
            await self._runner.setup()
            site = web.TCPSite(
                self._runner,
                self.config.host,
                self.config.port,
                ssl_context=self._ssl_context(),
            )
            await site.start()
        except BaseException:
            runner, self._runner = self._runner, None
            try:
                await runner.cleanup()
            except Exception:
                log.exception("failed to clean up HTTP server after startup failure")
            raise
        for url in self.page_urls:
            log.info("open in browser: %s", url)

    async def stop(self) -> None:
        for stream_id in list(self._audio_players):
            self._set_input_speaker(stream_id, False)
        sockets = list(self._signal_peers)
        self._signal_peers.clear()
        if sockets:
            try:
                await asyncio.wait_for(
                    asyncio.gather(
                        *(ws.close(code=1001, message=b"server shutdown") for ws in sockets),
                        return_exceptions=True,
                    ),
                    timeout=0.5,
                )
            except asyncio.TimeoutError:
                log.warning("timed out closing signaling WebSockets")
        peers = list(self._peers)
        self._peers.clear()
        if peers:
            try:
                await asyncio.wait_for(
                    asyncio.gather(
                        *(
                            module.detach(peer)
                            for peer in peers
                            for module in self.modules
                        ),
                        return_exceptions=True,
                    ),
                    timeout=1.0,
                )
            except asyncio.TimeoutError:
                log.warning("timed out detaching media tasks")
            try:
                await asyncio.wait_for(
                    asyncio.gather(*(pc.close() for pc in peers), return_exceptions=True),
                    timeout=1.0,
                )
            except asyncio.TimeoutError:
                log.warning("timed out closing WebRTC peers")
        if self._runner is not None:
            runner, self._runner = self._runner, None
            try:
                await asyncio.wait_for(runner.cleanup(), timeout=1.5)
            except asyncio.TimeoutError:
                log.warning("timed out cleaning up HTTP server")

    def start_background(self, timeout: float = 10.0) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._started.clear()
        self._start_error = None

        def run() -> None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            try:
                try:
                    self._loop.run_until_complete(self.start())
                except BaseException as exc:
                    self._start_error = exc
                    return
                self._started.set()
                self._loop.run_forever()
            finally:
                try:
                    self._loop.run_until_complete(self.stop())
                except Exception:
                    log.exception("failed to clean up media server")
                finally:
                    self._loop.close()
                    self._started.set()

        self._thread = threading.Thread(target=run, name="webrtc-media", daemon=True)
        self._thread.start()
        if not self._started.wait(timeout):
            raise TimeoutError("timed out starting MediaServer")
        if self._start_error:
            self._thread.join()
            raise RuntimeError("failed to start MediaServer") from self._start_error

    def stop_background(self, timeout: float = 4.0) -> None:
        loop, thread = self._loop, self._thread
        if loop and loop.is_running():
            future = asyncio.run_coroutine_threadsafe(self.stop(), loop)
            try:
                future.result(timeout=timeout)
            except concurrent.futures.TimeoutError:
                log.warning("timed out stopping media server")
                future.cancel()
            finally:
                loop.call_soon_threadsafe(loop.stop)
        if thread:
            thread.join(0.5)
            if thread.is_alive():
                log.warning("media server thread did not exit promptly")
                return
        self._thread = None
        self._loop = None
