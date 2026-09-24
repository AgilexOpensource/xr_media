import threading
from unittest.mock import Mock, patch

import numpy as np
import pytest

from xr_media.cli import _CaptureControl, _capture_worker, main


def test_unreadable_looping_file_stops_after_one_rewind():
    capture = Mock()
    capture.get.return_value = 30
    capture.read.return_value = (False, None)
    control = _CaptureControl(capture, "file")
    server = Mock()
    stop = Mock()
    stop.is_set.side_effect = [False] * 5 + [True]
    _capture_worker(server, stop, capture, "movie", "Movie", "file", 30, control)
    assert capture.read.call_count == 2
    capture.set.assert_called_once_with(1, 0)
    server.set_stream_status.assert_called_once_with("movie", "error")
    assert control.paused and control.ended


def test_successful_frames_allow_multiple_file_loops():
    capture = Mock()
    capture.get.return_value = 30
    image = np.zeros((2, 2, 3), dtype=np.uint8)
    capture.read.side_effect = [(True, image), (False, None), (True, image),
                                (False, None), (True, image)]
    control = _CaptureControl(capture, "file")
    server = Mock()
    stop = threading.Event()

    def received(*args):
        if server.push_video_frame.call_count == 3:
            stop.set()

    server.push_video_frame.side_effect = received
    _capture_worker(server, stop, capture, "movie", "Movie", "file", 30, control)
    assert capture.set.call_count == 2
    assert server.push_video_frame.call_count == 3
    server.set_stream_status.assert_not_called()


@pytest.mark.parametrize("failure", ["source", "server", "worker"])
def test_cli_failure_releases_capture_and_stops_server(failure):
    capture = Mock()
    with patch("xr_media.XrMediaServer") as server, patch(
        "xr_media.cli.require_dependencies"
    ), patch("xr_media.cli._open_capture", return_value=(capture, "camera", 0)), patch(
        "xr_media.cli.threading.Thread"
    ) as thread:
        thread.return_value.ident = None
        args = ["--stream", "camera:0"]
        if failure == "source":
            args += ["--stream", "video_test,invalid:1"]
        elif failure == "server":
            server.return_value.start_background.side_effect = RuntimeError("port busy")
        else:
            thread.return_value.start.side_effect = RuntimeError("thread failed")
        with pytest.raises((ValueError, RuntimeError)):
            main(args)
        capture.release.assert_called_once()
        thread.return_value.join.assert_not_called()
        server.return_value.stop_background.assert_called_once()


def test_unstarted_audio_file_is_closed_after_start_failure():
    container = Mock()
    with patch("xr_media.XrMediaServer") as server, patch(
        "xr_media.cli._open_audio_file", return_value=container
    ):
        server.return_value.start_background.side_effect = RuntimeError("port busy")
        with pytest.raises(RuntimeError):
            main(["--stream", "audio_file:sample.wav"])
        container.close.assert_called_once()
        server.return_value.stop_background.assert_called_once()


def test_started_workers_are_joined_after_later_worker_fails():
    started = Mock(ident=123)
    unstarted = Mock(ident=None)
    unstarted.start.side_effect = RuntimeError("thread failed")
    with patch("xr_media.XrMediaServer") as server, patch(
        "xr_media.cli.threading.Thread", side_effect=[started, unstarted]
    ):
        with pytest.raises(RuntimeError):
            main(["--stream", "video_test", "--stream", "audio_test"])
        started.join.assert_called_once_with(timeout=2.0)
        unstarted.join.assert_not_called()
        server.return_value.stop_background.assert_called_once()
