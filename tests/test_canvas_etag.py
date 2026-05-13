"""Canvas ETag derives from project-state fingerprint and short-circuits 304s."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from fastapi.testclient import TestClient

from unwind.server import create_app
from unwind import registry as _reg


def _setup_project(tmp_path: Path, monkeypatch, slug: str) -> tuple[str, str, Path]:
    monkeypatch.setenv("HOME", str(tmp_path))
    _reg.forget_slug(slug)
    sid = "11111111-2222-3333-4444-555555555555"
    proj_dir = tmp_path / ".claude" / "projects" / slug
    proj_dir.mkdir(parents=True)
    rec = {
        "uuid": "u1",
        "type": "user",
        "sessionId": sid,
        "timestamp": "2026-05-13T00:00:00.000Z",
        "message": {"role": "user", "content": "hi"},
    }
    (proj_dir / f"{sid}.jsonl").write_text(json.dumps(rec) + "\n")
    return slug, sid, proj_dir


def test_canvas_returns_etag_and_304(tmp_path, monkeypatch):
    slug, sid, _ = _setup_project(tmp_path, monkeypatch, "-canvas-etag-a")
    app = create_app()
    with TestClient(app) as c:
        r1 = c.get(f"/api/projects/{slug}/sessions/{sid}/canvas")
        assert r1.status_code == 200
        etag = r1.headers["ETag"]
        assert etag.startswith('"') and etag.endswith('"')

        r2 = c.get(
            f"/api/projects/{slug}/sessions/{sid}/canvas",
            headers={"If-None-Match": etag},
        )
        assert r2.status_code == 304
        assert r2.headers["ETag"] == etag
        # 304 body must be empty.
        assert r2.content == b""


def test_canvas_etag_changes_when_jsonl_changes(tmp_path, monkeypatch):
    slug, sid, proj_dir = _setup_project(tmp_path, monkeypatch, "-canvas-etag-b")
    app = create_app()
    with TestClient(app) as c:
        r1 = c.get(f"/api/projects/{slug}/sessions/{sid}/canvas")
        e1 = r1.headers["ETag"]

        # Touch the JSONL — mtime + size change.
        time.sleep(0.01)
        with (proj_dir / f"{sid}.jsonl").open("a") as fh:
            fh.write(
                json.dumps({
                    "uuid": "u2",
                    "type": "user",
                    "sessionId": sid,
                    "timestamp": "2026-05-13T00:00:01.000Z",
                    "message": {"role": "user", "content": "more"},
                }) + "\n"
            )
        future = time.time() + 1
        os.utime(proj_dir / f"{sid}.jsonl", (future, future))

        r2 = c.get(
            f"/api/projects/{slug}/sessions/{sid}/canvas",
            headers={"If-None-Match": e1},
        )
        # Fingerprint changed, so we must NOT 304.
        assert r2.status_code == 200
        assert r2.headers["ETag"] != e1
