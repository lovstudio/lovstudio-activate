"""lovstudio-skill-helper — activate and run paid Lovstudio skills locally."""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__ = _pkg_version("lovstudio-skill-helper")
except PackageNotFoundError:
    # Running from a source checkout without install.
    __version__ = "0.0.0+unknown"

