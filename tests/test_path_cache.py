"""Path-keyed cache for JSONL parsing (read_records, collect_uuids)."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from unwind import jsonl


def test_read_records_caches_until_mtime_changes(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text(json.dumps({"uuid": "u1"}) + "\n")
    a = jsonl.read_records(path)
    b = jsonl.read_records(path)
    # Same object identity proves the cache hit (vs. just same contents).
    assert a is b
    assert a == ({"uuid": "u1"},)

    # Bump mtime by writing fresh content.
    time.sleep(0.01)
    path.write_text(
        json.dumps({"uuid": "u1"}) + "\n" + json.dumps({"uuid": "u2"}) + "\n"
    )
    # Force a perceptible mtime change in case the FS rounds to seconds.
    future = time.time() + 1
    os.utime(path, (future, future))
    c = jsonl.read_records(path)
    assert c is not a
    assert c == ({"uuid": "u1"}, {"uuid": "u2"})


def test_collect_uuids_caches(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text(json.dumps({"uuid": "x"}) + "\n")
    a = jsonl.collect_uuids(path)
    b = jsonl.collect_uuids(path)
    assert a is b  # frozenset identity = cache hit
    assert a == {"x"}


def test_records_tuple_is_immutable(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text(json.dumps({"uuid": "u1"}) + "\n")
    recs = jsonl.read_records(path)
    assert isinstance(recs, tuple)


def test_path_cache_evicts_lru(tmp_path):
    from unwind._cache import PathCache

    calls: list[Path] = []

    def loader(p):
        calls.append(p)
        return p.read_text()

    cache = PathCache(loader, maxsize=2)
    a = tmp_path / "a"
    b = tmp_path / "b"
    c = tmp_path / "c"
    for p in (a, b, c):
        p.write_text(p.name)

    cache.get(a)  # store: [a]
    cache.get(b)  # store: [a, b]
    cache.get(a)  # touch a -> [b, a]
    cache.get(c)  # evicts b (LRU) -> [a, c]
    assert len(cache) == 2

    # b is gone, a is still cached.
    calls.clear()
    cache.get(a)
    cache.get(c)
    assert calls == []  # both hits
    cache.get(b)
    assert calls == [b]  # b had to be reloaded
