/** PNG export of the React Flow canvas.
 *
 * Why this lives outside CanvasPane: the export is a 150-line imperative
 * DOM/canvas script (style mutation, tiling, server stitching), not
 * presentation logic. Keeping it in its own module keeps the pane file
 * focused on rendering.
 *
 * Why the edge SVG needs special handling: React Flow's edges SVG
 * carries class ``react-flow__container``, whose CSS forces
 * ``width:100%; height:100%`` of the viewport — and the viewport is a
 * transform-only container with no intrinsic size. On screen, paths
 * render anyway via CSS ``overflow:visible``. During SVG rasterization
 * that overflow is ignored, so any path outside the SVG's userspace box
 * gets clipped — which is every edge. We temporarily give the edges SVG
 * an explicit width/height + viewBox covering the node bounds (inline
 * style beats the ``.react-flow__container`` CSS rule, attributes
 * don't), then undo it after capture.
 */
import { getNodesBounds, type ReactFlowInstance } from "reactflow";
import { toPng } from "html-to-image";

/** Browsers cap <canvas> on each side at ~16384px (Chrome/Safari). */
const MAX_CANVAS_DIM = 16384;
const PADDING = 48;
/** Fixed flow-unit → image-pixel ratio so text density is constant
 *  across exports (only pixelRatio scales it). */
const PIXEL_RATIO = 2;

type TilePiece = { blob: Blob; x: number; y: number };

/** Capture the React Flow viewport as a PNG and download it.
 *  Splits into tiles when the rasterized size exceeds the canvas cap,
 *  then ships the tiles to the server for ImageMagick to composite. */
export async function exportCanvasPng(
  reactFlow: ReactFlowInstance,
  filenameStem: string,
): Promise<void> {
  const internalNodes = reactFlow.getNodes();
  if (internalNodes.length === 0) return;
  const viewportEl = document.querySelector(
    ".react-flow__viewport",
  ) as HTMLElement | null;
  if (!viewportEl) return;

  const bounds = getNodesBounds(internalNodes);
  const width = Math.ceil(bounds.width + PADDING * 2);
  const height = Math.ceil(bounds.height + PADDING * 2);
  const vp = { zoom: 1, x: PADDING - bounds.x, y: PADDING - bounds.y };
  const bg = getComputedStyle(document.body).backgroundColor || "#0a0a0a";

  const restore = applyExportStyles(viewportEl, {
    vbX: Math.floor(bounds.x - PADDING),
    vbY: Math.floor(bounds.y - PADDING),
    vbW: width,
    vbH: height,
  });

  try {
    const tiles = await captureTiles(viewportEl, { width, height, vp, bg });
    if (tiles.length === 1) {
      triggerDownload(tiles[0].blob, filenameStem);
      return;
    }
    const stitched = await stitchOnServer(tiles, {
      output_width: width * PIXEL_RATIO,
      output_height: height * PIXEL_RATIO,
      background: bg,
    });
    triggerDownload(stitched, filenameStem);
  } finally {
    restore();
  }
}

/** Temporarily widen the edges SVG and boost edge contrast for capture.
 *  Returns a function that undoes every mutation. */
function applyExportStyles(
  viewportEl: HTMLElement,
  vb: { vbX: number; vbY: number; vbW: number; vbH: number },
): () => void {
  const restorers: Array<() => void> = [];

  const edgeSvgs = Array.from(
    viewportEl.querySelectorAll(".react-flow__edges"),
  ) as SVGSVGElement[];
  for (const svg of edgeSvgs) {
    const origStyle = svg.getAttribute("style");
    const origViewBox = svg.getAttribute("viewBox");
    svg.style.width = `${vb.vbW}px`;
    svg.style.height = `${vb.vbH}px`;
    svg.style.left = `${vb.vbX}px`;
    svg.style.top = `${vb.vbY}px`;
    svg.style.overflow = "visible";
    svg.setAttribute("viewBox", `${vb.vbX} ${vb.vbY} ${vb.vbW} ${vb.vbH}`);
    restorers.push(() => {
      if (origStyle !== null) svg.setAttribute("style", origStyle);
      else svg.removeAttribute("style");
      if (origViewBox !== null) svg.setAttribute("viewBox", origViewBox);
      else svg.removeAttribute("viewBox");
    });
  }

  // Live edge stroke is rgba(255,255,255,0.2) — hard to see in a
  // downscaled PNG. Boost opacity + width for the capture only.
  const isDark = document.documentElement.classList.contains("dark");
  const exportStroke = isDark
    ? "rgba(255, 255, 255, 0.28)"
    : "rgba(0, 0, 0, 0.22)";
  const paths = Array.from(
    viewportEl.querySelectorAll(".react-flow__edge-path"),
  ) as SVGPathElement[];
  for (const p of paths) {
    const origPathStyle = p.getAttribute("style");
    p.style.stroke = exportStroke;
    p.style.strokeWidth = "1.25";
    p.style.fill = "none";
    restorers.push(() => {
      if (origPathStyle !== null) p.setAttribute("style", origPathStyle);
      else p.removeAttribute("style");
    });
  }

  return () => {
    for (const r of restorers) r();
  };
}

async function captureTiles(
  viewportEl: HTMLElement,
  cfg: {
    width: number;
    height: number;
    vp: { zoom: number; x: number; y: number };
    bg: string;
  },
): Promise<TilePiece[]> {
  const { width, height, vp, bg } = cfg;
  const outW = width * PIXEL_RATIO;
  const outH = height * PIXEL_RATIO;
  const cols = Math.max(1, Math.ceil(outW / MAX_CANVAS_DIM));
  const rows = Math.max(1, Math.ceil(outH / MAX_CANVAS_DIM));
  const tileCssW = Math.ceil(width / cols);
  const tileCssH = Math.ceil(height / rows);

  const tiles: TilePiece[] = [];
  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      const thisCssW = c === cols - 1 ? width - c * tileCssW : tileCssW;
      const thisCssH = r === rows - 1 ? height - r * tileCssH : tileCssH;
      const dataUrl = await toPng(viewportEl, {
        backgroundColor: bg,
        width: thisCssW,
        height: thisCssH,
        pixelRatio: PIXEL_RATIO,
        style: {
          width: `${thisCssW}px`,
          height: `${thisCssH}px`,
          transform: `translate(${vp.x - c * tileCssW}px, ${vp.y - r * tileCssH}px) scale(${vp.zoom})`,
        },
      });
      const blob = await (await fetch(dataUrl)).blob();
      tiles.push({
        blob,
        x: c * tileCssW * PIXEL_RATIO,
        y: r * tileCssH * PIXEL_RATIO,
      });
    }
  }
  return tiles;
}

async function stitchOnServer(
  tiles: TilePiece[],
  manifest: { output_width: number; output_height: number; background: string },
): Promise<Blob> {
  const fd = new FormData();
  fd.append(
    "manifest",
    JSON.stringify({
      ...manifest,
      tiles: tiles.map((t) => ({ x: t.x, y: t.y })),
    }),
  );
  tiles.forEach((t, i) => fd.append("tile", t.blob, `tile_${i}.png`));

  const res = await fetch("/api/canvas-export-stitch", {
    method: "POST",
    body: fd,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`stitch failed: ${res.status} ${text}`);
  }
  return res.blob();
}

function triggerDownload(blob: Blob, filenameStem: string): void {
  const link = document.createElement("a");
  link.download = `unwind-canvas-${filenameStem}.png`;
  link.href = URL.createObjectURL(blob);
  link.click();
  URL.revokeObjectURL(link.href);
}
