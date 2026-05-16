"""Path-keyed memoization with (mtime, size) invalidation.

Used to cache the expensive results of JSONL parsing across requests. Caches
are process-global and thread-safe. Values are kept by reference — do not
mutate cached values.
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, TypeVar


T = TypeVar("T")

DEFAULT_MAXSIZE = 512


class PathCache:
    """LRU cache, one entry per Path, invalidated when (mtime, size) changes.

    Bounded to ``maxsize`` entries so a long-running process cannot retain
    every JSONL it ever parsed.
    """

    __slots__ = ("_lock", "_store", "_loader", "_maxsize")

    def __init__(
        self, loader: Callable[[Path], Any], maxsize: int = DEFAULT_MAXSIZE
    ) -> None:
        self._lock = threading.Lock()
        # Path -> (mtime, size, value); OrderedDict for LRU semantics.
        self._store: "OrderedDict[Path, tuple[float, int, Any]]" = OrderedDict()
        self._loader = loader
        self._maxsize = maxsize

    def get(self, path: Path) -> Any:
        try:
            st = path.stat()
        except OSError:
            return self._loader(path)
        sig = (st.st_mtime, st.st_size)
        with self._lock:
            existing = self._store.get(path)
            if existing is not None and (existing[0], existing[1]) == sig:
                self._store.move_to_end(path)
                return existing[2]
        value = self._loader(path)
        with self._lock:
            self._store[path] = (sig[0], sig[1], value)
            self._store.move_to_end(path)
            while len(self._store) > self._maxsize:
                self._store.popitem(last=False)
        return value

    def invalidate(self, path: Path) -> None:
        with self._lock:
            self._store.pop(path, None)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)
