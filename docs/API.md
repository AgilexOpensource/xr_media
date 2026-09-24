# SDK API reference

[English](API.md) | [简体中文](API.zh-CN.md) · [Back to README](../README.md)

This reference covers the `xr_media` Python API. See the README for installation, certificates and startup.

## Contents

- [Choose an interface and quick examples](#1-choose-an-interface-and-quick-examples)
- [Server configuration and lifecycle](#2-server-configuration-and-lifecycle)
- [Receive browser audio/video](#3-receive-browser-audiovideo)
- [Send audio/video to browsers](#4-send-audiovideo-to-browsers)
- [Frame formats and timestamps](#5-frame-formats-and-timestamps)
- [XR data](#6-xr-data)
- [Status and statistics](#7-status-and-statistics)
- [CLI, browser controls and access rules](#8-cli-browser-controls-and-access-rules)
- [Custom pages and HTTP endpoints (advanced)](#9-custom-pages-and-http-endpoints-advanced)

## 1. Choose an interface and quick examples

| Goal | Interface |
|---|---|
| Combined media and XR server | `XrMediaServer(config=None, on_xr=None)` |
| Media-only server | `MediaServer(config=None)` |
| Browser video/audio → Python | `register_browser_input()` + `add_video_listener()` / `add_audio_listener()` |
| Python video/audio → browser | `register_video_stream()` / `register_audio_stream()` + `push_video_frame()` / `push_audio_frame()` |
| Read XR | `on_xr(sample)` or `latest_xr` |

Import these types from `xr_media`. `XrMediaServer` inherits all `MediaServer` methods.
Register cards and listeners before startup. Reconnect existing pages after adding cards to renegotiate streams.

Run `xr_media --init-certs` manually first. Save each example as a separate Python file and run it.

### Receive audio/video and XR

Open a printed URL and start the uplink cards. Enter XR on the headset to receive poses.

```python
import time

from xr_media import AudioFrame, VideoFrame, XrMediaServer, XrSample


def on_video(stream_id: str, frame: VideoFrame):
    print(stream_id, frame.data.shape, frame.pixel_format)


def on_audio(stream_id: str, frame: AudioFrame):
    print(stream_id, frame.samples, frame.sample_rate)


def on_xr(sample: XrSample):
    if sample.hmd_tracked:
        print("headset", sample.hmd.px, sample.hmd.py, sample.hmd.pz)
    if sample.left.tracked:
        print("left trigger", sample.left.input.trigger_pressed)


server = XrMediaServer(on_xr=on_xr)
server.register_browser_input("camera", "video", "Camera")
server.register_browser_input("microphone", "audio", "Microphone")
server.add_video_listener(on_video)
server.add_audio_listener(on_audio)
server.start_background()
try:
    for url in server.page_urls:
        print(url)
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    pass
finally:
    server.stop_background()
```

### Send images and audio

This sends green images and silent PCM; replace the arrays with your data.

```python
import time
import numpy as np
from xr_media import AudioFrame, VideoFrame, XrMediaServer

server = XrMediaServer()
server.register_video_stream("image", "Image")
server.register_audio_stream("sound", "Sound")
server.start_background()
try:
    for url in server.page_urls:
        print(url)
    while True:
        stamp = time.monotonic_ns()
        image = np.zeros((240, 320, 3), dtype=np.uint8)
        image[:, :, 1] = 180
        server.push_video_frame(VideoFrame(image, 320, 240, "bgr24", stamp), "image")
        pcm = np.zeros(320, dtype=np.int16)
        server.push_audio_frame(AudioFrame(pcm, timestamp_ns=stamp), "sound")
        time.sleep(0.02)
except KeyboardInterrupt:
    pass
finally:
    server.stop_background()
```

## 2. Server configuration and lifecycle

### Configuration

`MediaServerConfig` is an immutable dataclass passed to the server constructor.

| Field | Default | Meaning |
|---|---|---|
| `host` | `"0.0.0.0"` | Listening address |
| `port` | `9443` | 1–65535 |
| `cert_path` / `key_path` | `None` | `Path`; defaults to `cert.pem` and `key.pem` in the user configuration directory |
| `codec` | `"h264"` | `h264`, `vp8`, `vp9` |
| `ice_servers` | `()` | Sequence of `(url, username, credential)` tuples; use `None` for unused credentials |
| `max_clients` | `1` | Positive integer; total media connection limit |
| `video_bitrate_min` | `1_000_000` | Minimum video bitrate, bit/s |
| `video_bitrate_default` | `3_000_000` | Initial video bitrate, bit/s |
| `video_bitrate_max` | `6_000_000` | Maximum video bitrate, bit/s |

Bitrates must satisfy `100000 <= min <= default <= max` and affect encoders in the same process. Invalid port, bitrate, client limit or codec values raise `ValueError`.
The server does not generate certificates; run `xr_media --init-certs` manually. The current implementation uses HTTP if either certificate file is missing.

### Start and stop

| Interface | Return | Usage |
|---|---|---|
| `start_background(timeout=10.0)` | `None` | Start a network thread for a regular Python application; timeout in seconds |
| `stop_background(timeout=4.0)` | `None` | Pair with background startup; call in `finally` |
| `await start()` | `None` | Start in an existing asyncio event loop |
| `await stop()` | `None` | Pair with async startup |
| `page_urls` | `list[str]` | Local network URLs using the current HTTP/HTTPS scheme |

Choose one lifecycle style; do not mix them. `start_background()` raises `TimeoutError` on startup wait timeout and `RuntimeError` on initialization failure. Shutdown timeouts are logged as warnings.

### asyncio example

In an existing event loop, use this pattern. `start()` returns after initialization; the application keeps the loop running. Cancellation or exit calls `stop()`.

```python
import asyncio
from xr_media import XrMediaServer


async def main():
    server = XrMediaServer()
    try:
        await server.start()
        print(*server.page_urls, sep="\n")
        await asyncio.Event().wait()
    finally:
        await server.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
```

## 3. Receive browser audio/video

```python
server.register_browser_input(stream_id, media_type, label="", on_control=None)
server.add_video_listener(callback)
server.add_audio_listener(callback)
server.remove_video_listener(callback)
server.remove_audio_listener(callback)
```

These methods return `None`.

- `stream_id`: nonempty; letters, digits, `_` and `-` only. Use different IDs for different cards.
- `media_type`: `"video"` or `"audio"`; invalid types or IDs raise `ValueError`.
- `label`: display name; defaults to the ID when empty.
- `on_control(action, value)`: optional regular function for uplink actions such as `start`, `pause`, `resume`, `stop`, `ended`, `preview` and `speaker`. Start carries the source; preview/speaker carry booleans; other values are usually `None`.
- Media callback signature: `callback(stream_id, frame)`; return values are ignored. Regular and `async` functions are supported. Pass the original function object to remove a listener.

`VideoFrame.data` is a BGR NumPy array; `AudioFrame.data` is a 1D int16 PCM array. Callbacks receive decoded frames accepted for forwarding. Uplink cards have exclusive ownership; pause retains ownership, while stop or disconnect cleanup releases it.

Regular callbacks run in threads; async media callbacks run in the network event loop. Keep callbacks short, synchronize shared data and use a separate worker queue for expensive work. Callback errors are logged rather than returned to the caller.

Each receive task awaits media listeners sequentially; tasks for different tracks or clients may run concurrently. Removing a listener does not cancel an active invocation or one already captured in a dispatch snapshot. `on_control` must be a regular function; it runs in a thread, not as an async callback.

| Uplink action | `value` | Meaning |
|---|---|---|
| `start` | Source string | Browser started the selected source |
| `pause` / `resume` | `None` | Pause / resume |
| `stop` / `ended` | `None` | Stop / source ended |
| `preview` / `speaker` | `bool` | PC preview / speaker toggle |

These are notifications; the application need not reopen browser devices. The internal ownership request `claim` is not delivered to the control callback.

## 4. Send audio/video to browsers

| Method | Return | Meaning |
|---|---|---|
| `register_video_stream(stream_id, label="", kind="", on_control=None)` | `None` | Register video; empty kind uses `live` for new streams and preserves existing kinds |
| `push_video_frame(frame, stream_id="main", label="")` | `None` | Store the latest video frame; not a browser display acknowledgment |
| `register_audio_stream(stream_id, label="", kind="audio")` | `None` | Register audio |
| `push_audio_frame(frame, stream_id="audio", label="")` | `None` | Store a PCM block |

Push methods register missing streams automatically; prefer registration before browser connection. Video keeps only the latest frame, so not every push is displayed. Do not mutate an image array after pushing it; copy reusable capture buffers.

Video is sent as new frames arrive, without an SDK frame-rate cap. Without a new frame, sending waits rather than repeating the last frame. Capture, encoding and network capacity determine the delivered rate.

`kind` describes the source; it does not open a device or file and is not restricted to an enum:

| Media | Built-in values | Behavior |
|---|---|---|
| Video | `live`, `camera`, `ros`, `video_test` | Regular video card; a new stream with `kind=""` uses `live` |
| Video | `file` | Shows file controls; the application handles requests through `on_control` |
| Audio | `audio`, `audio_test`, `ros` | Audio source label; defaults to `audio` |

Custom strings are allowed and use the regular card behavior. Register the kind before pushing frames; pushing does not change it. Re-registering video with `kind=""` preserves its existing kind.

**Repeated registration:** Video updates nonempty `label`, `kind` and non-`None` `on_control`; audio updates nonempty `label` and `kind`, with omitted kind defaulting to `audio`. Re-registering an uplink ID resets its card state, so register before startup. Use globally unique IDs across media types and directions to avoid browser mapping conflicts; do not rely on the server rejecting duplicates.

**Audio timing and buffering:** Push at sample-duration intervals, such as 320 samples every 20 ms, not an entire file at once. Each stream currently retains at most 200 blocks, discarding the oldest when full. Capacity is measured in blocks. Clients read independently; a new connection starts with subsequent blocks, and an empty buffer produces 320 silent samples. Slow readers may lose discarded blocks. Do not mutate sample arrays after pushing.

`kind="file"` exposes file controls. `on_control(action, value)` receives playback requests; the application implements source pause/resume behavior. Metadata does not read or play files.

| Downlink action | `value` | Application responsibility |
|---|---|---|
| `play` | `None` | Resume reading the source |
| `pause` | `None` | Pause reading the source |
| `stop` | `None` | Stop and return to the beginning |
| `replay` | `None` | Return to the beginning and play |

These are the current page's file controls; there is no seek slider. The channel can forward other actions without implementing them in the player. `loop` is supported internally by CLI sources, not exposed as a page button.

Pass requests to the capture worker instead of doing expensive work in the callback:

```python
from queue import SimpleQueue

commands = SimpleQueue()

def on_control(action, value):
    commands.put((action, value))

server.register_video_stream("movie", kind="file", on_control=on_control)
```

The capture worker consumes `commands`, implements `play`, `pause`, `stop`, `replay` and `loop`, then updates the playback state below. This example receives commands; it does not implement a file player.

```python
server.set_stream_status("movie", "paused")
server.set_stream_playback("movie", position=12.0, duration=60.0,
                           paused=True, ended=False, loop=False)
```

Valid statuses: `waiting`, `live`, `paused`, `seeking`, `ended`, `error`. Invalid status raises `ValueError`; a missing stream raises `KeyError`. Position and duration use seconds; methods return `None`. These APIs update video metadata, not locally uploaded browser files.

## 5. Frame formats and timestamps

### VideoFrame

`VideoFrame(data, width, height, pixel_format="bgr24", timestamp_ns=0)`

| Field | Meaning |
|---|---|
| `data` | NumPy array or tightly packed bytes; color arrays usually have shape `(height, width, 3)` |
| `width` / `height` | Positive dimensions in pixels |
| `pixel_format` | `bgr24`, `rgb24`, `gray8`, `i420`, `nv12` |
| `timestamp_ns` | Nanosecond timestamp; defaults to 0, preferably supplied by the producer |

Invalid dimensions or formats raise `ValueError`. Construction does not fully validate payload length; ensure the data matches its dimensions and format.

All video arrays use `uint8`; byte payloads are tightly packed without row padding. `H` and `W` are the original height and width:

| Format | Array shape | Bytes | Layout |
|---|---|---|---|
| `bgr24` / `rgb24` | `(H, W, 3)` | `H × W × 3` | BGR / RGB |
| `gray8` | `(H, W)` | `H × W` | Grayscale |
| `i420` | `(H × 3 / 2, W)` | `H × W × 3 / 2` | Consecutive Y, U, V planes |
| `nv12` | `(H × 3 / 2, W)` | `H × W × 3 / 2` | Y plane followed by interleaved UV |

`i420` and `nv12` require even dimensions. Other formats pad odd edges to even dimensions when sending; the received image may gain a row or column.

### AudioFrame

`AudioFrame(data, sample_rate=16000, channels=1, timestamp_ns=0)`

Use a 1D NumPy `int16` array for `data`. Only 16000 Hz, mono, nonempty blocks are supported; invalid values raise `ValueError`. The read-only `samples` property returns the sample count. 320 samples represent 20 ms, not 320 bytes.

Supply raw PCM samples, not WAV file bytes or compressed MP3 data; decode files first.

Received timestamps come from the media timeline, with a local monotonic-clock fallback. Do not interpret them as UTC or use them as cross-device synchronization guarantees.

## 6. XR data

The `XrMediaServer(on_xr=callback)` callback is `callback(sample: XrSample)`. Use a regular function; return values are ignored. `latest_xr` is `None` before the first sample and then retains the last sample; it does not guarantee current tracking.

| Type | Fields |
|---|---|
| `XrSample` | `t_ms=0`, `headset_id="headset"`, `hmd`, `left`, `right`, `hmd_tracked=False`, `passthrough=False` |
| `RigidPose` | `px/py/pz=0.0` in metres; `qx/qy/qz=0.0`, `qw=1.0`, quaternion x/y/z/w |
| `TrackedHand` | `pose`, `input`, `tracked=False`, `source_type="none"`, `joints={}` |
| `HandInput` | Buttons and analog values listed below |

| Input fields | Type | Default |
|---|---|---|
| `trigger_pressed`, `grip_pressed` | `bool` | `False` |
| `stick_clicked`, `face_primary`, `face_secondary` | `bool` | `False` |
| `trigger_analog`, `grip_analog` | `float` | `0.0` |
| `stick_x`, `stick_y` | `float` | `0.0` |

`hmd` is a `RigidPose`; `left/right` are `TrackedHand` objects. `joints` maps joint names to `RigidPose` and may be empty. Check tracking flags before using poses. `t_ms` is the client sample time in milliseconds, not guaranteed to be synchronized with the PC clock.

The browser converts WebXR poses to Z-up. Hand pinch maps to `trigger_pressed` and `trigger_analog`. `source_type` identifies the input source, such as `controller`, `hand` or `none`.

Parsing helpers: `RigidPose.from_mapping(data)`, `HandInput.from_mapping(data)`, `TrackedHand.from_mapping(data)` and `XrSample.from_json(payload)`. The last accepts a parsed dictionary, not a JSON string. Parsed defaults do not imply valid tracking.

## 7. Status and statistics

| Interface | Result |
|---|---|
| `list_streams()` | `list[dict]` of downlink video streams, not all audio/uplink cards |
| `get_stats()` | `MediaServerStats` |
| `get_xr_stats()` | XR received/dispatched/replaced counts; `XrMediaServer` only |
| `GET /api/streams` | `{"streams": [...]}` including video, audio and uplink cards |
| `GET /api/stats` | JSON fields corresponding to `MediaServerStats` |
| `GET /api/xr/state` | `{"xr": sample or null, "session": {...}}`; `XrMediaServer` only |

`MediaServerStats` fields: `clients`, `frames_received`, `frames_replaced`, `frames_sent`, `latest_timestamp_ns`, `streams`, `audio_frames_received`, `audio_frames_sent`. `clients` counts media peers, not all reserved admission seats. Video received/replaced counts refer to downlink frame buffers; replacements are not network packet loss.

`get_xr_stats()` returns `xr_server_received`, `xr_server_dispatched`, `xr_server_replaced`. HTTP endpoints have no user authentication; use trusted networks.

### Response examples

Example video entry from `list_streams()` after one frame is pushed:

```json
{
  "id": "main",
  "label": "main",
  "media_type": "video",
  "ready": true,
  "status": "live",
  "kind": "live",
  "playback": {},
  "width": 640,
  "height": 480,
  "frames_received": 1,
  "frames_replaced": 0,
  "capture_fps": 0.0
}
```

Convert `get_stats()` to a dictionary with `dataclasses.asdict(server.get_stats())`.
Before the first XR sample, `/api/xr/state` returns:

```json
{
  "xr": null,
  "session": {
    "mode": null,
    "passthrough": false,
    "preview": false,
    "ui_locked": false
  }
}
```

## 8. CLI, browser controls and access rules

### Initialize certificates

See [Generate certificates](../README.md#generate-certificates) for the command.
Each run generates and overwrites `cert.pem` and `key.pem`, regardless of their existence or expiry. Server startup does not generate certificates. After replacement, restart the server and trust the new certificate on viewing devices.

Development certificates are stored in `$XDG_CONFIG_HOME/xr_media/tls`, or
`~/.config/xr_media/tls` if that variable is unset. The server discovers them
automatically. Configure trust on the viewing device as required by its browser.
Remote camera/microphone access and WebXR need a secure context; without
certificates, the server may use HTTP.

### CLI sources

Repeat `--stream [label=]source[,key:value...]` to add cards.

| Source | Direction | Options / behavior |
|---|---|---|
| `video_test` | PC → browser | `width`, `height`; generates at 30 Hz |
| `audio_test` | PC → browser | Test audio |
| `camera[:index]` | PC → browser | `width`, `height`, `fourcc` (default `MJPG`); no added rate cap |
| `video_file:path` | PC → browser | Original dimensions and file rate; warns and falls back to 30 Hz if invalid |
| `audio_file:path` | PC → browser | Decodes and loops audio |
| `video_input` | Browser → PC | Browser video source |
| `audio_input` | Browser → PC | Browser audio source |

`video_file:` selects video only, without audio. `audio_file:` selects the first
audio track (including in video containers), decodes to 16 kHz mono PCM, and
loops at its sample rate.
To expose both tracks, register the file twice with distinct labels and prefixes; these independent cards do not provide synchronized A/V playback.

### Browser controls and multiple clients

| Item | Behavior |
|---|---|
| Admission | `--max-clients` is a positive integer, default 1; cards initialize after admission |
| Full server | Shows count, limit, connection IDs and browser types; retry after a seat is released |
| Video/audio uplink | One client per card; separate cards have separate owners |
| Uplink pause/stop | Pause retains ownership; stop or disconnect cleanup releases it; disconnect detection is not immediate |
| Video/audio downlink | Admitted clients can receive together; source playback controls may affect other viewers |
| XR | One XR session at a time; exit or disconnect releases it |

Select `Device` or `File` on an uplink card, then start. `PC View` requests a
preview on the server computer. Uplink audio `PC Out` controls that computer's
speaker and requires `paplay` or `aplay`. On a downlink audio card, `PC Out`
controls the current browser's speaker, not the remote PC.
Audio starts muted; playback or waveform analysis may require user interaction.
XR needs a compatible headset/browser; desktop browsers can still use media cards.

### Common issues

Missing Python dependencies produce an error listing the packages and an installation command for the current Python environment; the SDK never installs them automatically. `--help` and `--init-certs` work without media libraries; certificate generation still requires Bash and OpenSSL. OpenCV is checked only for cameras or local video files.

| Symptom | Action |
|---|---|
| Camera or XR unavailable | Check HTTPS, certificate trust and browser permissions |
| Full server or occupied card | Wait for the owner to leave, then retry |
| No audio | Click play; check mute, volume and output device |
| Video stays on the last frame | Check that the producer keeps calling `push_video_frame()` |
| Camera/file startup requires OpenCV | Install the `opencv` extra; see the README |

### Limitations

- No user authentication; use trusted networks. Capacity and card ownership do not replace authorization.
- Set the listening address with `--host`; do not expose the service directly to the Internet.
- Use trusted TLS certificates and protect private keys.

## 9. Custom pages and HTTP endpoints (advanced)

Subclass the server to add HTTP endpoints or replace web assets. Adding media cards only requires the registration methods, not subclassing.

### Add an HTTP endpoint

Save and run this example after generating certificates as described in the README. Open `/api/app/status` under a printed URL to query application status; press `Ctrl+C` to exit.

```python
import time
from aiohttp import web
from xr_media import XrMediaServer


class AppServer(XrMediaServer):
    def configure_app(self, app):
        super().configure_app(app)
        app.router.add_get("/api/app/status", self.app_status)

    async def app_status(self, request):
        return web.json_response({"running": True})


server = AppServer()
try:
    server.start_background()
    print(*server.page_urls, sep="\n")
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    pass
finally:
    server.stop_background()
```

`configure_app()` runs during startup, before listening begins. Keep the `super()` call to register XR routes. Use an application prefix such as `/api/app/`; do not register duplicate `/`, `/signal`, `/xr-signal` or existing `/api/` paths.

Handlers use `async def` and return aiohttp responses. Keep blocking capture and expensive computation outside handlers. Custom endpoints do not automatically inherit authentication or card ownership checks; authorize sensitive operations yourself.

### Replace the media page

This example applies to `MediaServer`. Place custom assets in `web/` beside your script, then run `AppServer()` using the startup and shutdown pattern above:

```python
from pathlib import Path
from xr_media import MediaServer


class AppServer(MediaServer):
    def asset_root(self):
        return Path(__file__).resolve().parent / "web"
```

```text
app.py
web/
  index.html
  app.css
  client.js
```

`/` serves `web/index.html`; `/static/` exposes the entire `web/` directory. To retain the default interface, copy `index.html`, `app.css` and `client.js` from the SDK's `static/` directory and preserve the relationship between page elements and scripts. Place additional assets here too, but never private keys or configuration credentials.

**XR distinction:** `XrMediaServer` currently reads its built-in index template directly and injects XR resources; `/xr/` also serves built-in assets. Overriding `asset_root()` only replaces `/static/`, not the XR index or `/xr/`. Replacing the entire XR page requires additional page-handler and resource-route customization beyond these two hooks.
