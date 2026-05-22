"""POST /api/canvas-export-stitch — stitch browser-rendered tiles into one PNG.

The browser splits the canvas into tiles to stay under the ~16384px raster
canvas limit, uploads them with a manifest of pixel offsets, and we shell out
to ImageMagick (``magick``) to composite them onto a single output image.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

router = APIRouter(tags=["canvas-export"])


@router.post("/canvas-export-stitch")
async def canvas_export_stitch(
    manifest: str = Form(...),
    tile: list[UploadFile] = File(...),
) -> Response:
    """Composite uploaded tiles onto a single ``WxH`` PNG.

    ``manifest`` is a JSON form field with::

        {
          "output_width":  int,        # output PNG width in pixels
          "output_height": int,        # output PNG height in pixels
          "background":    str,        # any CSS/Magick color
          "tiles":         [{ "x": int, "y": int }, ...]  # one per uploaded tile
        }

    The order of ``tile`` files must match the order of ``tiles`` entries.
    """
    try:
        m: dict[str, Any] = json.loads(manifest)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"manifest not JSON: {e}")
    out_w = int(m.get("output_width", 0))
    out_h = int(m.get("output_height", 0))
    bg = str(m.get("background") or "black")
    positions = m.get("tiles") or []
    if out_w <= 0 or out_h <= 0:
        raise HTTPException(400, "invalid output dimensions")
    if len(tile) != len(positions):
        raise HTTPException(
            400,
            f"tile count mismatch: {len(tile)} files vs {len(positions)} positions",
        )
    # Bound the request — magick will happily allocate gigabytes otherwise.
    if out_w * out_h > 600_000_000:  # ~600 MP, ~2.4 GB RGBA
        raise HTTPException(413, "requested image too large")

    with tempfile.TemporaryDirectory(prefix="unwind-export-") as td:
        td_path = Path(td)
        tile_paths: list[Path] = []
        for i, t in enumerate(tile):
            p = td_path / f"tile_{i:03d}.png"
            p.write_bytes(await t.read())
            tile_paths.append(p)

        out_path = td_path / "out.png"
        cmd: list[str] = [
            "magick",
            "-size",
            f"{out_w}x{out_h}",
            f"xc:{_magick_color(bg)}",
        ]
        for tp, pos in zip(tile_paths, positions):
            x = int(pos.get("x", 0))
            y = int(pos.get("y", 0))
            cmd += [str(tp), "-geometry", f"+{x}+{y}", "-composite"]
        cmd.append(str(out_path))

        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=180)
        except FileNotFoundError:
            raise HTTPException(
                500,
                "ImageMagick (`magick`) not on PATH on the unwind server",
            )
        except subprocess.TimeoutExpired:
            raise HTTPException(500, "ImageMagick timed out")
        except subprocess.CalledProcessError as e:
            err = (e.stderr or b"").decode("utf-8", "replace")[:500]
            raise HTTPException(500, f"ImageMagick failed: {err}")

        return Response(content=out_path.read_bytes(), media_type="image/png")


def _magick_color(css: str) -> str:
    """Normalize a CSS color for ImageMagick's ``xc:`` source.

    Browser ``getComputedStyle`` returns ``rgb(R, G, B)`` with spaces, which
    Magick rejects — strip them. Falls back to ``black`` for empty input.
    """
    s = css.strip().replace(" ", "")
    return s or "black"
