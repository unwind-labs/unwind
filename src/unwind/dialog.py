"""Show a native folder-picker dialog on the host OS.

The FastAPI server runs on the user's local machine, so we can drive a real
GUI dialog from a request handler. Tkinter must run on the main thread on
macOS, so we always invoke the picker in a fresh subprocess and read the
chosen path off stdout. Returns ``None`` when the user cancels.
"""
from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional


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
        return None
    osa = shutil.which("osascript")
    if not osa:
        return None
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
    proc = subprocess.run(
        [osa, "-e", script],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        # User cancelled (-128) or another osascript error — treat as no-op.
        return None
    chosen = proc.stdout.strip()
    return chosen or None


def _pick_with_tk(initial: Optional[str]) -> Optional[str]:
    proc = subprocess.run(
        [sys.executable, "-c", _TK_SCRIPT, initial or ""],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        return None
    chosen = proc.stdout.strip()
    return chosen or None


def pick_folder(initial: Optional[str] = None) -> Optional[Path]:
    """Open a folder picker; return the absolute path or ``None`` if cancelled."""
    chosen = _pick_with_osascript(initial) or _pick_with_tk(initial)
    if not chosen:
        return None
    return Path(chosen).expanduser().resolve()
