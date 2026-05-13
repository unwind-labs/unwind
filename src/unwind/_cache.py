"""Path-keyed memoization with (mtime, size) invalidation.

Used to cache the expensive results of JSONL parsing across requests. Caches
are process-global and thread-safe. Values are kept by reference — do not
mutate cached values.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable, TypeVar


T = TypeVar("T")


class PathCache:
    """Cache one entry per Path, invalidated when (mtime, size) changes.

    The cache is bounded only by the number of distinct Paths ever passed —
    fine for unwind (one entry per session JSONL).
    """

    __slots__ = ("_lock", "_store", "_loader")

    def __init__(self, loader: Callable[[Path], Any]) -> None:
        self._lock = threading.Lock()
        # Path -> (mtime, size, value)
        self._store: dict[Path, tuple[float, int, Any]] = {}
        self._loader = loader

    def get(self, path: Path) -> Any:
        try:
            st = path.stat()
        except OSError:
            # Surface to caller; the underlying loader should also handle this.
            return self._loader(path)
        sig = (st.st_mtime, st.st_size)
        with self._lock:
            existing = self._store.get(path)
            if existing is not None and (existing[0], existing[1]) == sig:
                return existing[2]
        value = self._loader(path)
        with self._lock:
            self._store[path] = (sig[0], sig[1], value)
        return value

    def invalidate(self, path: Path) -> None:
        with self._lock:
            self._store.pop(path, None)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
