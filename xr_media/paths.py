import os
import sys
from pathlib import Path


def static_dir() -> Path:
    source = Path(__file__).resolve().parent.parent / "static"
    if source.is_dir():
        return source
    try:
        from ament_index_python.packages import get_package_share_directory

        installed = Path(get_package_share_directory("xr_media")) / "static"
        if installed.is_dir():
            return installed
    except (ImportError, LookupError):
        pass
    installed = Path(sys.prefix) / "share" / "xr_media" / "static"
    if installed.is_dir():
        return installed
    return source


def tls_dir() -> Path:
    """Return the per-user directory containing development TLS files."""
    config_home = os.environ.get("XDG_CONFIG_HOME")
    root = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return root / "xr_media" / "tls"
