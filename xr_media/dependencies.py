"""Actionable errors for missing Python dependencies."""

from importlib import import_module
import shlex
import sys


def require_dependencies(*packages):
    package_names = dict(packages)
    missing = []
    for module in package_names:
        try:
            import_module(module)
        except ModuleNotFoundError as exc:
            if exc.name not in package_names:
                raise
            missing_package = package_names[exc.name]
            if missing_package not in missing:
                missing.append(missing_package)
    if missing:
        command = shlex.join([sys.executable, "-m", "pip", "install", *missing])
        raise ModuleNotFoundError(
            "Missing dependencies: %s. Install in this Python environment:\n%s"
            % (", ".join(missing), command)
        )
