"""Cross-platform helpers for launching external programs from the TUI.

Spawning a fresh terminal window keeps the monitor running while the
user works in Claude Code. Both the main view and the detail screens
need this, so it lives in its own module to avoid a tui ↔ detail-screen
import cycle.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path


def open_terminal_with(cwd: str, command: list[str]) -> tuple[bool, str]:
    """Spawn a new terminal window in cwd running command (detached).

    Returns (ok, message). Cross-platform best-effort:
    - $TERMINAL env var if set
    - macOS: osascript + Terminal.app
    - Windows: start cmd
    - Linux/BSD: tries kitty / alacritty / wezterm / gnome-terminal /
      konsole / xterm / x-terminal-emulator in that order
    """
    if sys.platform == "darwin":
        cmd_str = " ".join(shlex.quote(c) for c in command)
        script = (
            f'tell application "Terminal" to do script '
            f'"cd {shlex.quote(cwd)} && {cmd_str}"'
        )
        try:
            subprocess.Popen(["osascript", "-e", script])
            return True, "Opened in Terminal.app"
        except Exception as e:
            return False, f"osascript failed: {e}"

    if sys.platform == "win32":
        cmd_str = " ".join(shlex.quote(c) for c in command)
        try:
            subprocess.Popen(
                f'start "" cmd /k "cd /d \"{cwd}\" && {cmd_str}"',
                shell=True,
            )
            return True, "Opened in cmd"
        except Exception as e:
            return False, f"start failed: {e}"

    candidates: list[str] = []
    env_term = os.environ.get("TERMINAL")
    if env_term:
        candidates.append(env_term)
    candidates += [
        "kitty", "alacritty", "wezterm",
        "gnome-terminal", "konsole",
        "xterm", "x-terminal-emulator",
    ]
    for term in candidates:
        if not shutil.which(term):
            continue
        try:
            if term == "kitty":
                subprocess.Popen([term, "--directory", cwd, *command])
            elif term == "alacritty":
                subprocess.Popen(
                    [term, "--working-directory", cwd, "-e", *command]
                )
            elif term == "wezterm":
                subprocess.Popen(
                    [term, "start", "--cwd", cwd, "--", *command]
                )
            elif term == "gnome-terminal":
                subprocess.Popen(
                    [term, "--working-directory", cwd, "--", *command]
                )
            elif term == "konsole":
                subprocess.Popen([term, "--workdir", cwd, "-e", *command])
            else:
                subprocess.Popen([term, "-e", *command], cwd=cwd)
            return True, f"Opened in {term}"
        except Exception as e:
            return False, f"{term} failed: {e}"
    return False, "No terminal emulator found in PATH"


def open_in_file_manager(path: str | None) -> tuple[bool, str]:
    """xdg-open on Linux, 'open' on macOS, explorer on Windows."""
    if not path or not Path(path).is_dir():
        return False, "Path missing on disk"
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", path])
        elif sys.platform == "win32":
            subprocess.Popen(["explorer", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return True, f"Opened {path}"
    except Exception as e:
        return False, f"Open failed: {e}"
