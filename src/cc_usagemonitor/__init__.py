"""cc-monitor — real-time token usage monitor for Claude Code sessions.

Version is read from package metadata so a single source-of-truth
(pyproject.toml) drives everything: --version, the Settings screen
diagnostics block, and the PyPI update-checker. Falls back to a sentinel
when running from an uninstalled checkout (e.g. `python -m
cc_usagemonitor` directly out of src/) so the value is still defined.
"""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("cc-monitor")
except PackageNotFoundError:
    __version__ = "0.0.0+local"
