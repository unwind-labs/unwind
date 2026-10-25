"""Show a native folder-picker dialog on the host OS.

The FastAPI server runs on the user's local machine, so we can drive a real
GUI dialog from a request handler. Tkinter must run on the main thread on
macOS, so we always invoke the picker in a fresh subprocess and read the
chosen path off stdout. Returns ``None`` when the user cancels.
"""

from __future__ import annotations

import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional


class FolderPickerUnavailable(RuntimeError):
    """The native picker could not be started or failed."""


class FolderPickerTimeout(RuntimeError):
    """The native picker did not finish within its time limit."""


def _run_picker(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired as exc:
        raise FolderPickerTimeout(
            "The folder picker timed out after 120 seconds. Please try again."
        ) from exc
    except OSError as exc:
        raise FolderPickerUnavailable(
            "The folder picker could not be started."
        ) from exc


_TK_SCRIPT = """
import sys
try:
    import tkinter as tk
    from tkinter import filedialog
except Exception as exc:  # pragma: no cover - depends on host Python build
    sys.stderr.write(f"tkinter unavailable: {exc}\\n")
    sys.exit(2)

initial = sys.argv[1] if len(sys.argv) > 1 else ""
root = tk.Tk()
root.withdraw()
try:
    root.attributes("-topmost", True)
except tk.TclError:
    pass
kwargs = {"title": "Pick a project folder", "mustexist": True}
if initial:
    kwargs["initialdir"] = initial
path = filedialog.askdirectory(**kwargs)
sys.stdout.write(path or "")
"""


def _escape_applescript_string(s: str) -> str:
    """Escape a string for safe embedding inside an AppleScript "..." literal.

    AppleScript string literals only recognise ``\\\\`` and ``\\"`` as escapes,
    so we strip control characters (which terminate the script anyway) and
    backslash-escape both metacharacters.
    """
    cleaned = "".join(ch for ch in s if ord(ch) >= 0x20)
    return cleaned.replace("\\", "\\\\").replace('"', '\\"')


def _pick_with_osascript(initial: Optional[str]) -> Optional[str]:
    if platform.system() != "Darwin":
        raise FolderPickerUnavailable("AppleScript is only available on macOS.")
    osa = shutil.which("osascript")
    if not osa:
        raise FolderPickerUnavailable("osascript is unavailable.")
    choose = 'choose folder with prompt "Pick a project folder"'
    if initial:
        safe = _escape_applescript_string(initial)
        # AppleScript needs a POSIX path coerced to an alias.
        choose += f' default location (POSIX file "{safe}")'
    # Present the dialog *inside* a System Events tell-block and ``activate``
    # it. ``unwind serve`` runs as a background process with no UI activation
    # policy, so a dialog it spawns directly opens without focus — behind the
    # browser or on another Space — and looks to the user like nothing
    # happened. System Events is a faceless app that can host the picker and
    # be brought to the foreground, so the dialog reliably appears in front.
    script = (
        'tell application "System Events"\n'
        "    activate\n"
        f"    set chosenFolder to {choose}\n"
        "end tell\n"
        "POSIX path of chosenFolder"
    )
    proc = _run_picker([osa, "-e", script])
    if proc.returncode != 0:
        # Cancellation is final, not a reason to open the fallback picker.
        if re.search(r"\(-128\)\s*$", proc.stderr):
            return None
        raise FolderPickerUnavailable("The macOS folder picker could not open.")
    chosen = proc.stdout.strip()
    return chosen or None


def _pick_with_tk(initial: Optional[str]) -> Optional[str]:
    proc = _run_picker([sys.executable, "-c", _TK_SCRIPT, initial or ""])
    if proc.returncode != 0:
        raise FolderPickerUnavailable(
            "The folder picker is unavailable. Choose a project from the project list."
        )
    chosen = proc.stdout.strip()
    return chosen or None


def pick_folder(initial: Optional[str] = None) -> Optional[Path]:
    """Return a path or None on cancel; raise a typed error on failure.

    Fall back to Tk only when AppleScript is unavailable or fails, never after
    cancellation or timeout. subprocess.run kills and reaps timed-out children.
    """
    try:
        chosen = _pick_with_osascript(initial)
    except FolderPickerUnavailable:
        chosen = _pick_with_tk(initial)
    if not chosen:
        return None
    return Path(chosen).expanduser().resolve()
