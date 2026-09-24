import sys
import subprocess
import types
import threading
import wave
from unittest import mock

import numpy as np
import pytest

from xr_media.cli import _audio_file_worker, _capture_worker, _open_audio_file, _open_capture, parser
from xr_media.cli import main


def test_module_entrypoint_help():
    result = subprocess.run(
        [sys.executable, "-m", "xr_media.cli", "--help"],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0
    assert "--stream" in result.stdout
    assert "--init-certs" in result.stdout
    assert "--fps" not in result.stdout
    assert "maximum admitted browser clients" in result.stdout
    assert "requested camera width" in result.stdout
    assert "requested camera height" in result.stdout
    assert "HTTP/HTTPS port" in result.stdout


def test_init_certs_does_not_start_server(tmp_path, monkeypatch):
    monkeypatch.setenv("XR_MEDIA_TLS_DIR", str(tmp_path))
    with mock.patch("xr_media.cli.subprocess.run") as run, mock.patch(
        "xr_media.XrMediaServer"
    ) as server:
        assert main(["--init-certs"]) == 0
        run.assert_called_once()
        assert run.call_args.args[0][0] == "bash"
        assert run.call_args.kwargs["env"]["XR_MEDIA_TLS_DIR"] == str(tmp_path)
        server.assert_not_called()


def test_init_certs_overwrites_existing_files_on_each_run(tmp_path, monkeypatch):
    monkeypatch.setenv("XR_MEDIA_TLS_DIR", str(tmp_path))
    for name in ("cert.pem", "key.pem"):
        (tmp_path / name).write_text("old")
    previous = b"old"
    for _ in range(2):
        assert main(["--init-certs"]) == 0
        certificate = (tmp_path / "cert.pem").read_bytes()
        assert b"BEGIN CERTIFICATE" in certificate
        assert certificate != previous
        assert b"PRIVATE KEY" in (tmp_path / "key.pem").read_bytes()
        previous = certificate


def test_init_certs_reports_missing_command(tmp_path, monkeypatch):
    monkeypatch.setenv("XR_MEDIA_TLS_DIR", str(tmp_path))
    with mock.patch("xr_media.cli.subprocess.run", side_effect=FileNotFoundError):
        assert main(["--init-certs"]) == 1


class FakeCapture:
    def __init__(self, target):
        self.target = target
        self.settings = []

    def isOpened(self):
        return True

    def set(self, key, value):
        self.settings.append((key, value))
        return True

    def release(self):
        pass

    def get(self, key):
        return 24.0


def test_camera_stream_opens_numeric_index_with_defaults():
    fake_cv2 = types.SimpleNamespace(
        VideoCapture=FakeCapture,
        VideoWriter_fourcc=lambda *value: value,
        CAP_PROP_FOURCC=1,
        CAP_PROP_FRAME_WIDTH=2,
        CAP_PROP_FRAME_HEIGHT=3,
        CAP_PROP_FPS=4,
    )
    args = parser().parse_args([])
    with mock.patch.dict(sys.modules, {"cv2": fake_cv2}):
        capture, kind, fps = _open_capture("camera:0", {}, args)
    assert capture.target == 0
    assert kind == "camera"
    assert fps == 0.0
    assert capture.settings == [
        (1, ("M", "J", "P", "G")),
        (2, 640),
        (3, 480),
    ]


def test_video_file_prefix():
    with mock.patch.dict(sys.modules, {"cv2": types.SimpleNamespace(VideoCapture=FakeCapture, CAP_PROP_FPS=4)}):
        capture, kind, fps = _open_capture("video_file:movie.mp4", {}, parser().parse_args([]))
    assert capture.target == "movie.mp4"
    assert kind == "file"
    assert fps == 24


@pytest.mark.parametrize("rate", [0, -1, float("nan"), float("inf")])
def test_invalid_file_rate_falls_back(rate):
    fake_cv2 = types.SimpleNamespace(VideoCapture=FakeCapture, CAP_PROP_FPS=4)
    with mock.patch.dict(sys.modules, {"cv2": fake_cv2}), mock.patch.object(
        FakeCapture, "get", return_value=rate
    ):
        _, _, fps = _open_capture("video_file:movie.mp4", {}, parser().parse_args([]))
    assert fps == 30


def test_removed_fps_argument_is_rejected():
    with pytest.raises(SystemExit):
        parser().parse_args(["--fps", "30"])


def test_removed_fps_source_option_is_rejected():
    with mock.patch.dict(sys.modules, {"cv2": types.SimpleNamespace()}):
        with pytest.raises(ValueError, match="unknown stream option: fps"):
            _open_capture("camera:0", {"fps": "30"}, parser().parse_args([]))


@pytest.mark.parametrize("kind,rate", [("camera", 0), ("file", 24)])
def test_capture_worker_only_paces_files(kind, rate):
    stop = threading.Event()
    server = mock.Mock()
    capture = mock.Mock()
    capture.read.return_value = (True, np.zeros((2, 2, 3), dtype=np.uint8))
    server.push_video_frame.side_effect = lambda *args: stop.set()
    with mock.patch.object(stop, "wait", return_value=True) as wait, mock.patch(
        "xr_media.cli.time.monotonic", return_value=10.0
    ):
        _capture_worker(server, stop, capture, "test", "Test", kind, rate, None)
    server.push_video_frame.assert_called_once()
    if kind == "camera":
        wait.assert_not_called()
    else:
        assert wait.call_args.args[0] == pytest.approx(1 / rate)


def test_legacy_file_prefix_is_not_supported():
    with mock.patch.dict(sys.modules, {"cv2": None}):
        assert _open_capture("file:movie.mp4", {}, parser().parse_args([])) is None


def test_audio_file_resamples_and_loops(tmp_path):
    path = tmp_path / "stereo.wav"
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(48000)
        output.writeframes(np.full((4800, 2), 1000, dtype=np.int16).tobytes())
    stop = threading.Event()
    frames = []

    def receive(frame, stream_id, label):
        assert (stream_id, label) == ("music", "Music")
        frames.append(frame)
        if sum(len(item.data) for item in frames) > 1600:
            stop.set()

    container = _open_audio_file("audio_file:" + str(path))
    worker = threading.Thread(target=_audio_file_worker, args=(
        types.SimpleNamespace(push_audio_frame=receive), stop, container, "music", "Music"))
    worker.start()
    worker.join(timeout=3)
    stop.set()
    worker.join(timeout=1)
    assert not worker.is_alive()
    assert sum(len(frame.data) for frame in frames) > 1600
    assert all(frame.sample_rate == 16000 and frame.channels == 1 for frame in frames)
    assert all(frame.data.dtype == np.int16 for frame in frames)


def test_audio_file_rejects_missing_path_or_track():
    with pytest.raises(ValueError, match="path cannot be empty"):
        _open_audio_file("audio_file:")
    container = mock.Mock()
    container.streams.audio = []
    with mock.patch("av.open", return_value=container):
        with pytest.raises(ValueError, match="no audio track"):
            _open_audio_file("audio_file:video.mp4")
    container.close.assert_called_once()
