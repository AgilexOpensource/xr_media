"""Run the combined WebRTC media and WebXR card interface."""

import argparse
import logging
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import time

from .media import AudioFrame, MediaServerConfig, VideoFrame

from .dependencies import require_dependencies
from .paths import static_dir, tls_dir


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--init-certs", action="store_true",
        help="generate and overwrite development TLS certificates, then exit (requires Bash and OpenSSL)",
    )
    result.add_argument(
        "--stream", action="append", default=[],
        help=("add a card: [label=]source[,key:value...]; repeat for multiple cards. "
              "Sources: video_test (default), audio_test, camera[:index], "
              "video_file:path, audio_file:path, video_input, audio_input"),
    )
    result.add_argument("--host", default="0.0.0.0",
                        help="listen address (default: %(default)s)")
    result.add_argument("--port", type=int, default=9443,
                        help="HTTP/HTTPS port (default: %(default)s)")
    result.add_argument("--max-clients", type=int, default=1,
                        help="maximum admitted browser clients (default: %(default)s)")
    result.add_argument("--width", type=int, default=640,
                        help="default test-video width / requested camera width in pixels (default: %(default)s)")
    result.add_argument("--height", type=int, default=480,
                        help="default test-video height / requested camera height in pixels (default: %(default)s)")
    return result


def _parse(spec: str):
    head, *options = [part.strip() for part in spec.split(",") if part.strip()]
    label, source = head.split("=", 1) if "=" in head else (head, head)
    values = {}
    for option in options:
        if ":" not in option:
            raise ValueError("stream option must be key:value: %s" % option)
        key, value = option.split(":", 1)
        values[key.strip()] = value.strip()
    return label.strip(), source.strip(), values


def _video_test(server, stop, width, height, stream_id="video_test", label="video_test"):
    import numpy as np

    interval = 1.0 / 30
    index = 0
    while not stop.is_set():
        frame = np.zeros((height, width, 3), dtype=np.uint8)
        band = max(1, width // 6)
        colors = ((235, 235, 235), (0, 210, 240), (220, 190, 0), (20, 190, 80), (210, 40, 170), (40, 80, 220))
        for column, color in enumerate(colors):
            frame[:, column * band:(column + 1) * band] = color
        marker = (index * 8) % width
        frame[max(0, height // 2 - 12):height // 2 + 12, max(0, marker - 12):marker + 12] = 10
        server.push_video_frame(VideoFrame(frame, width, height, "bgr24", time.monotonic_ns()), stream_id, label)
        index += 1
        stop.wait(interval)


def _audio_test(server, stop):
    import numpy as np

    rate = 16000
    count = 320
    phase = 0
    while not stop.is_set():
        positions = np.arange(count, dtype=np.float64) + phase
        pcm = (np.sin(2 * np.pi * 440 * positions / rate) * 6000).astype(np.int16)
        server.push_audio_frame(AudioFrame(pcm, rate, 1, time.monotonic_ns()), "audio_test", "audio_test")
        phase += count
        stop.wait(count / rate)


def _open_audio_file(source):
    import av

    path = source.split(":", 1)[1]
    if not path:
        raise ValueError("audio file path cannot be empty")
    container = av.open(path)
    if not container.streams.audio:
        container.close()
        raise ValueError("file has no audio track: %s" % path)
    return container


def _audio_file_worker(server, stop, container, stream_id, label):
    from av import AudioResampler

    try:
        while not stop.is_set():
            resampler = AudioResampler(format="s16", layout="mono", rate=16000)
            deadline = time.monotonic()
            produced = False
            for frame in container.decode(audio=0):
                for block in resampler.resample(frame):
                    if stop.wait(max(0.0, deadline - time.monotonic())):
                        return
                    server.push_audio_frame(AudioFrame(
                        block.to_ndarray().reshape(-1), 16000, 1,
                        time.monotonic_ns(),
                    ), stream_id, label)
                    deadline += block.samples / 16000
                    produced = True
            for block in resampler.resample(None):
                if stop.wait(max(0.0, deadline - time.monotonic())):
                    return
                server.push_audio_frame(AudioFrame(
                    block.to_ndarray().reshape(-1), 16000, 1,
                    time.monotonic_ns(),
                ), stream_id, label)
                deadline += block.samples / 16000
                produced = True
            if not produced or stop.wait(max(0.0, deadline - time.monotonic())):
                return
            container.seek(0)
    except Exception:
        logging.exception("audio file stream failed: %s", stream_id)
    finally:
        container.close()


class _CaptureControl:
    def __init__(self, capture, kind):
        self.capture = capture
        self.kind = kind
        self.lock = threading.RLock()
        self.paused = False
        self.ended = False
        self.loop = kind == "file"

    def control(self, action, value=None):
        if self.kind != "file":
            return
        with self.lock:
            if action == "pause":
                self.paused = True
            elif action in {"play", "replay"}:
                if action == "replay" or self.ended:
                    self.capture.set(1, 0)  # cv2.CAP_PROP_POS_FRAMES
                self.paused = False
                self.ended = False
            elif action == "stop":
                self.capture.set(1, 0)
                self.paused = True
                self.ended = False
            elif action == "loop":
                self.loop = True

    def snapshot(self):
        with self.lock:
            fps = self.capture.get(5)  # cv2.CAP_PROP_FPS
            frames = self.capture.get(7)  # cv2.CAP_PROP_FRAME_COUNT
            position = self.capture.get(0)  # cv2.CAP_PROP_POS_MSEC
            return {
                "position": max(0.0, position / 1000.0),
                "duration": frames / fps if fps > 0 and frames > 0 else 0.0,
                "paused": self.paused,
                "ended": self.ended,
                "loop": self.loop,
                "seekable": True,
            }


def _capture_worker(server, stop, capture, stream_id, label, kind, fps, control):
    interval = 1.0 / fps if kind == "file" else 0.0
    deadline = time.monotonic()
    rewound = False
    while not stop.is_set():
        if control is not None:
            with control.lock:
                paused = control.paused
            if paused:
                server.set_stream_status(stream_id, "paused")
                server.set_stream_playback(stream_id, **control.snapshot())
                stop.wait(0.05)
                deadline = time.monotonic()
                continue
        ok, image = capture.read()
        if not ok:
            if control is not None and control.loop and not rewound:
                with control.lock:
                    capture.set(1, 0)
                    control.ended = False
                rewound = True
                continue
            if control is not None:
                with control.lock:
                    control.ended = True
                    control.paused = True
            status = "ended" if kind == "file" and not rewound else "error"
            server.set_stream_status(stream_id, status)
            if rewound:
                logging.error("Video file cannot be read after rewind: %s", stream_id)
            if control is not None:
                server.set_stream_playback(stream_id, **control.snapshot())
            return
        rewound = False
        height, width = image.shape[:2]
        server.push_video_frame(
            VideoFrame(image, width, height, "bgr24", time.monotonic_ns()),
            stream_id, label,
        )
        if control is not None:
            server.set_stream_playback(stream_id, **control.snapshot())
        if kind == "file":
            deadline += interval
            delay = deadline - time.monotonic()
            if delay > 0:
                stop.wait(delay)
            elif delay < -interval:
                deadline = time.monotonic()


def _open_capture(source, options, args):
    if not (source == "camera" or source.startswith(("camera:", "video_file:"))):
        return None
    require_dependencies(("cv2", "opencv-python-headless"))
    import cv2
    allowed = {"width", "height", "fourcc"}
    unknown = set(options) - allowed
    if unknown:
        raise ValueError("unknown stream option: %s" % sorted(unknown)[0])
    width = int(options.get("width", args.width))
    height = int(options.get("height", args.height))
    if width <= 0 or height <= 0:
        raise ValueError("width and height must be positive")
    if source == "camera" or source.startswith("camera:"):
        target = source.split(":", 1)[1] if ":" in source else "0"
        target = int(target) if target.isdigit() else target
        kind = "camera"
    elif source.startswith("video_file:"):
        target = source.split(":", 1)[1]
        if not target:
            raise ValueError("file stream path cannot be empty")
        kind = "file"
    else:
        return None
    capture = cv2.VideoCapture(target)
    if not capture.isOpened():
        capture.release()
        raise RuntimeError("cannot open video source: %s" % target)
    if kind == "camera":
        fourcc = options.get("fourcc", "MJPG").upper()
        if fourcc != "AUTO":
            if len(fourcc) != 4:
                capture.release()
                raise ValueError("camera FOURCC must contain four characters or be 'auto'")
            capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc))
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    fps = 0.0
    if kind == "file":
        fps = float(capture.get(cv2.CAP_PROP_FPS))
        if not math.isfinite(fps) or fps <= 0:
            logging.warning("Video file has no valid frame rate; using 30 Hz: %s", target)
            fps = 30.0
    return capture, kind, fps


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="[xr-media] %(message)s")
    if args.init_certs:
        target = Path(os.environ.get("XR_MEDIA_TLS_DIR") or tls_dir()).expanduser()
        script = static_dir().parent / "scripts" / "make_dev_certs.sh"
        try:
            subprocess.run(
                ["bash", str(script)], check=True,
                env={**os.environ, "XR_MEDIA_TLS_DIR": str(target)},
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            logging.error(
                "Certificate generation failed: %s. Check Bash and OpenSSL; "
                "on Ubuntu/Debian: sudo apt install bash openssl", exc
            )
            return 1
        return 0
    specs = args.stream or ["video_test"]
    parsed = [_parse(raw) for raw in specs]
    try:
        from . import XrMediaServer

        if any(source == "camera" or source.startswith(("camera:", "video_file:"))
               for _label, source, _options in parsed):
            require_dependencies(("cv2", "opencv-python-headless"))
    except ModuleNotFoundError as exc:
        logging.error("%s", exc)
        return 1
    server = XrMediaServer(
        MediaServerConfig(host=args.host, port=args.port,
                          max_clients=args.max_clients),
    )
    workers = []
    captures = []
    audio_files = []
    stop = threading.Event()
    used_ids = set()
    video_started = False
    audio_started = False
    try:
        for label, source, options in parsed:
            base_id = re.sub(r"[^A-Za-z0-9_-]+", "_", label).strip("_") or source
            stream_id = base_id
            suffix = 2
            while stream_id in used_ids:
                stream_id = "%s_%d" % (base_id, suffix)
                suffix += 1
            used_ids.add(stream_id)
            if source == "video_input":
                server.register_browser_input(stream_id, "video", label)
            elif source == "audio_input":
                server.register_browser_input(stream_id, "audio", label)
            elif source == "video_test":
                if video_started:
                    raise ValueError("video_test may be registered once")
                video_started = True
                unknown = set(options) - {"width", "height"}
                if unknown:
                    raise ValueError("unknown stream option: %s" % sorted(unknown)[0])
                width = int(options.get("width", args.width))
                height = int(options.get("height", args.height))
                if width <= 0 or height <= 0:
                    raise ValueError("width and height must be positive")
                server.register_video_stream(stream_id, label, kind="video_test")
                workers.append(threading.Thread(
                    target=_video_test, args=(server, stop, width, height, stream_id, label),
                    daemon=True,
                ))
            elif source == "audio_test":
                if audio_started:
                    raise ValueError("audio_test may be registered once")
                audio_started = True
                server.register_audio_stream("audio_test", label or "audio_test", "audio_test")
                workers.append(threading.Thread(target=_audio_test, args=(server, stop), daemon=True))
            elif source.startswith("audio_file:"):
                container = _open_audio_file(source)
                worker = threading.Thread(
                    target=_audio_file_worker,
                    args=(server, stop, container, stream_id, label), daemon=True,
                )
                audio_files.append((container, worker))
                workers.append(worker)
                server.register_audio_stream(stream_id, label)
            else:
                opened = _open_capture(source, options, args)
                if opened is None:
                    raise ValueError("unsupported integrated stream: %s" % source)
                capture, kind, fps = opened
                captures.append(capture)
                control = _CaptureControl(capture, kind)
                server.register_video_stream(
                    stream_id, label, kind=kind,
                    on_control=control.control if kind == "file" else None,
                )
                if kind == "file":
                    server.set_stream_playback(stream_id, **control.snapshot())
                workers.append(threading.Thread(
                    target=_capture_worker,
                    args=(server, stop, capture, stream_id, label, kind, fps, control),
                    daemon=True,
                ))
        server.start_background()
        for worker in workers:
            worker.start()
        for url in server.page_urls:
            print("Open in headset browser: %s" % url, flush=True)
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        while not stop.wait(0.25):
            pass
    finally:
        stop.set()
        try:
            for worker in workers:
                if worker.ident is not None:
                    worker.join(timeout=2.0)
            for capture in captures:
                capture.release()
            for container, worker in audio_files:
                if worker.ident is None:
                    container.close()
        finally:
            server.stop_background()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
