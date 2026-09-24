import os
from pathlib import Path
import subprocess
import sys
from unittest import mock

import pytest

from xr_media.dependencies import require_dependencies


def test_missing_dependencies_include_install_command():
    def missing(name):
        raise ModuleNotFoundError(name, name=name)

    with mock.patch("xr_media.dependencies.import_module", side_effect=missing):
        with pytest.raises(ModuleNotFoundError) as error:
            require_dependencies(("cv2", "opencv-python-headless"), ("av", "av"))
    assert "-m pip install opencv-python-headless av" in str(error.value)
    assert sys.executable in str(error.value)


def test_broken_dependency_is_not_reported_as_missing_package():
    error = ModuleNotFoundError("internal module missing", name="internal_module")
    with mock.patch("xr_media.dependencies.import_module", side_effect=error):
        with pytest.raises(ModuleNotFoundError) as caught:
            require_dependencies(("av", "av"))
    assert caught.value is error


def test_known_transitive_missing_dependency_is_reported_once():
    error = ModuleNotFoundError("numpy missing", name="numpy")
    with mock.patch("xr_media.dependencies.import_module", side_effect=error):
        with pytest.raises(ModuleNotFoundError) as caught:
            require_dependencies(("numpy", "numpy"), ("aiortc", "aiortc"))
    assert str(caught.value).endswith("-m pip install numpy")


@pytest.mark.parametrize("action", ["help", "certs", "run", "sdk"])
def test_entrypoints_without_media_dependencies(action, tmp_path):
    script = '''
import importlib.abc
import sys
from unittest.mock import patch

class BlockMedia(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in {"numpy", "aiohttp", "av", "aiortc"}:
            raise ModuleNotFoundError("blocked: " + fullname, name=fullname)

sys.meta_path.insert(0, BlockMedia())
from xr_media.cli import main
action = sys.argv[1]
if action == "help":
    main(["--help"])
elif action == "certs":
    with patch("xr_media.cli.subprocess.run") as run:
        assert main(["--init-certs"]) == 0
        run.assert_called_once()
elif action == "sdk":
    from xr_media import XrMediaServer
else:
    raise SystemExit(main([]))
'''
    result = subprocess.run(
        [sys.executable, "-S", "-c", script, action],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
             "XR_MEDIA_TLS_DIR": str(tmp_path)},
        capture_output=True, text=True, timeout=15,
    )
    if action in {"help", "certs"}:
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode != 0
        assert "-m pip install numpy aiohttp av aiortc" in result.stderr
        if action == "run":
            assert "Traceback" not in result.stderr
