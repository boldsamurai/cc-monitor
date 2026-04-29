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

from .logger import get_logger

log = get_logger(__name__)


def _detached_popen_kwargs() -> dict:
    """Run subprocess fully detached so its stdio doesn't bleed into our
    Textual TUI (which would otherwise show a spurious error flash in
    the notification area when the spawned process writes anything to
    stderr/stdout). On POSIX we also start a new session so the child
    survives our exit."""
    kw: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        # DETACHED_PROCESS = 0x00000008 — child runs without a console
        kw["creationflags"] = 0x00000008
    else:
        kw["start_new_session"] = True
    return kw


def open_terminal_with(cwd: str, command: list[str]) -> tuple[bool, str]:
    """Spawn a new terminal window in cwd running command (detached).

    Returns (ok, message). Cross-platform best-effort:
    - $TERMINAL env var if set
    - macOS: osascript + Terminal.app
    - Windows: start cmd
    - Linux/BSD: tries kitty / alacritty / wezterm / gnome-terminal /
      konsole / xterm / x-terminal-emulator in that order
    """
    log.info("open_terminal_with cwd=%s cmd=%s", cwd, command)
    detach = _detached_popen_kwargs()

    if sys.platform == "darwin":
        cmd_str = " ".join(shlex.quote(c) for c in command)
        script = (
            f'tell application "Terminal" to do script '
            f'"cd {shlex.quote(cwd)} && {cmd_str}"'
        )
        try:
            subprocess.Popen(["osascript", "-e", script], **detach)
            return True, "Opened in Terminal.app"
        except Exception as e:
            log.error("osascript failed: %s", e)
            return False, f"osascript failed: {e}"

    if sys.platform == "win32":
        cmd_str = " ".join(shlex.quote(c) for c in command)
        try:
            subprocess.Popen(
                f'start "" cmd /k "cd /d \"{cwd}\" && {cmd_str}"',
                shell=True,
                **detach,
            )
            return True, "Opened in cmd"
        except Exception as e:
            log.error("start cmd failed: %s", e)
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
    last_error = ""
    for term in candidates:
        if not shutil.which(term):
            continue
        try:
            if term == "kitty":
                subprocess.Popen(
                    [term, "--directory", cwd, *command], **detach
                )
            elif term == "alacritty":
                subprocess.Popen(
                    [term, "--working-directory", cwd, "-e", *command],
                    **detach,
                )
            elif term == "wezterm":
                subprocess.Popen(
                    [term, "start", "--cwd", cwd, "--", *command], **detach
                )
            elif term == "gnome-terminal":
                subprocess.Popen(
                    [term, "--working-directory", cwd, "--", *command],
                    **detach,
                )
            elif term == "konsole":
                subprocess.Popen(
                    [term, "--workdir", cwd, "-e", *command], **detach
                )
            else:
                subprocess.Popen(
                    [term, "-e", *command], cwd=cwd, **detach
                )
            return True, f"Opened in {term}"
        except Exception as e:
            # Don't stop on the first failure — try the next candidate.
            last_error = f"{term} failed: {e}"
            log.warning("%s — trying next candidate", last_error)
            continue
    final = last_error or "No terminal emulator found in PATH"
    log.error(final)
    return False, final


def open_file(path: str | Path) -> tuple[bool, str]:
    """Open a file in the user's default app — text editor for .log/.md,
    image viewer for images, etc. Same xdg-open / open / start dispatch
    as open_in_file_manager but for files instead of directories."""
    log.info("open_file path=%s", path)
    p = Path(path)
    if not p.exists():
        log.warning("open_file: file not found: %s", p)
        return False, f"File not found: {p}"
    detach = _detached_popen_kwargs()
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(p)], **detach)
        elif sys.platform == "win32":
            subprocess.Popen(f'start "" "{p}"', shell=True, **detach)
        else:
            subprocess.Popen(["xdg-open", str(p)], **detach)
        return True, f"Opened {p}"
    except Exception as e:
        log.error("open_file failed: %s", e)
        return False, f"Open failed: {e}"


def open_in_file_manager(path: str | None) -> tuple[bool, str]:
    """xdg-open on Linux, 'open' on macOS, explorer on Windows."""
    log.info("open_in_file_manager path=%s", path)
    if not path or not Path(path).is_dir():
        log.warning("open_in_file_manager: path missing or not a dir: %s", path)
        return False, "Path missing on disk"
    detach = _detached_popen_kwargs()
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", path], **detach)
        elif sys.platform == "win32":
            subprocess.Popen(["explorer", path], **detach)
        else:
            subprocess.Popen(["xdg-open", path], **detach)
        return True, f"Opened {path}"
    except Exception as e:
        log.error("open_in_file_manager failed: %s", e)
        return False, f"Open failed: {e}"
