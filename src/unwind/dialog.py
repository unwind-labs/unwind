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

root = tk.Tk()
root.withdraw()
try:
    root.attributes("-topmost", True)
except tk.TclError:
    pass
path = filedialog.askdirectory(title="Pick a project folder", mustexist=True)
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
    parts = ['choose folder with prompt "Pick a project folder"']
    if initial:
        safe = _escape_applescript_string(initial)
        # AppleScript needs a POSIX path coerced to an alias.
        parts.append(f'default location (POSIX file "{safe}")')
    script = f'POSIX path of ({" ".join(parts)})'
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
    script = _TK_SCRIPT
    if initial:
        script = (
            script.replace(
                'filedialog.askdirectory(title="Pick a project folder", mustexist=True)',
                f'filedialog.askdirectory(title="Pick a project folder", mustexist=True, initialdir={initial!r})',
            )
        )
    proc = subprocess.run(
        [sys.executable, "-c", script],
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
