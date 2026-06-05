import type { CanvasTreeResponse, WindowNode } from "@/api/types";

/** One visible line in the folder-tree view: a window node plus the
 *  presentation facts the renderer and keyboard nav need. ``collapsed`` is
 *  whether THIS node's children are hidden (the node itself still shows). */
export type FlatRow = {
  node: WindowNode;
  depth: number;
  hasChildren: boolean;
  collapsed: boolean;
};

/** Pre-order DFS over ``node.children`` (the backend's structural order). A
 *  collapsed node still appears as a row; its descendants are skipped. This
 *  is the single source of "what's visible right now" — both rendering and
 *  ↑/↓/←/→ navigation derive from the same flat list, so they can never
 *  disagree about row order or what counts as the next/previous item. */
export function flattenTree(root: WindowNode, collapsed: ReadonlySet<string>): FlatRow[] {
  const rows: FlatRow[] = [];
  const walk = (node: WindowNode, depth: number) => {
    const hasChildren = node.children.length > 0;
    const isCollapsed = collapsed.has(node.window_id);
    rows.push({ node, depth, hasChildren, collapsed: isCollapsed });
    if (hasChildren && !isCollapsed) {
      for (const child of node.children) walk(child, depth + 1);
    }
  };
  walk(root, 0);
  return rows;
}

/** Next focused window_id for a vertical (↑/↓) move, clamped at both ends.
 *  Entering with no focus (``focusedId === null``) lands on the first row so
 *  the first keypress always has somewhere to go. Returns ``null`` only when
 *  there are no rows at all. */
export function nextRowId(rows: FlatRow[], focusedId: string | null, dir: 1 | -1): string | null {
  if (rows.length === 0) return null;
  const idx = focusedId ? rows.findIndex((r) => r.node.window_id === focusedId) : -1;
  if (idx < 0) return rows[0].node.window_id;
  const next = Math.min(rows.length - 1, Math.max(0, idx + dir));
  return rows[next].node.window_id;
}

export type RightActionResult =
  | { type: "expand"; id: string }
  | { type: "focus"; id: string }
  | { type: "none" };

/** What → (or ``l``) should do, mirroring a file-tree's expand semantics:
 *  on a leaf nothing happens; on a collapsed parent it expands; on an
 *  already-expanded parent it dives into the first child. The caller applies
 *  the result (mutate the collapsed set or move focus). */
export function rightAction(
  rows: FlatRow[],
  focusedId: string | null,
  collapsed: ReadonlySet<string>,
): RightActionResult {
  const idx = focusedId ? rows.findIndex((r) => r.node.window_id === focusedId) : -1;
  if (idx < 0) return { type: "none" };
  const row = rows[idx];
  if (!row.hasChildren) return { type: "none" };
  if (collapsed.has(row.node.window_id)) return { type: "expand", id: row.node.window_id };
  // Expanded with children → focus the first child. With this node expanded,
  // its first child is the very next row at depth+1.
  const child = rows[idx + 1];
  if (child && child.depth === row.depth + 1) return { type: "focus", id: child.node.window_id };
  return { type: "none" };
}

export type LeftActionResult =
  | { type: "collapse"; id: string }
  | { type: "focus"; id: string }
  | { type: "none" };

/** What ← (or ``h``) should do: collapse an open parent, otherwise step out
 *  to the parent row. ``none`` at the root tells the caller there's nowhere
 *  left to go (the folder-tree pane uses that to hand focus back to the
 *  session list, matching the canvas's ← behaviour). */
export function leftAction(
  rows: FlatRow[],
  focusedId: string | null,
  collapsed: ReadonlySet<string>,
): LeftActionResult {
  const idx = focusedId ? rows.findIndex((r) => r.node.window_id === focusedId) : -1;
  if (idx < 0) return { type: "none" };
  const row = rows[idx];
  // The root has no parent, and collapsing it would hide the whole run behind
  // one row — useless. Treat ← on the root as "nowhere to go" so the pane
  // hands focus back to the session list (matching the canvas's ← behaviour).
  if (row.depth === 0) return { type: "none" };
  if (row.hasChildren && !collapsed.has(row.node.window_id)) {
    return { type: "collapse", id: row.node.window_id };
  }
  // Leaf or already-collapsed → step out to the nearest preceding row one
  // level shallower (the parent).
  for (let i = idx - 1; i >= 0; i--) {
    if (rows[i].depth === row.depth - 1) return { type: "focus", id: rows[i].node.window_id };
  }
  return { type: "none" };
}

/** A call tree's nesting depth (root alone → 0, root→child → 1, …). */
export function maxDepth(node: WindowNode): number {
  if (node.children.length === 0) return 0;
  let deepest = 0;
  for (const child of node.children) deepest = Math.max(deepest, maxDepth(child));
  return deepest + 1;
}

/** Count of nodes reachable from ``root`` via ``children`` — i.e. exactly
 *  what the tree (and the canvas) actually renders. NOT ``all_windows.length``:
 *  ``all_windows`` can carry orphan windows (``parent_window_id === null``,
 *  unreachable from root) that neither view shows — the canvas filters them
 *  out the same way (it only lays out nodes reachable from root). */
export function treeSize(node: WindowNode): number {
  let n = 1;
  for (const child of node.children) n += treeSize(child);
  return n;
}

// Thresholds past which the graphical canvas starts to need a lot of
// panning/zooming and the lightweight text view is the better default.
// Either a wide run (many cards) OR a deep one (long call chain) qualifies.
export const COMPLEX_TREE_NODE_COUNT = 20;
export const COMPLEX_TREE_MAX_DEPTH = 4;

/** True when a run is involved enough that the folder-tree view should be the
 *  automatic default. Measured on the RENDERED tree (``treeSize`` /
 *  ``maxDepth`` from root), not ``all_windows`` — orphan windows that never
 *  appear on the canvas must not inflate the count and force text view onto
 *  an actually-simple run. Cheap — runs off the already-fetched tree. */
export function isComplexTree(tree: CanvasTreeResponse): boolean {
  return (
    treeSize(tree.root) > COMPLEX_TREE_NODE_COUNT || maxDepth(tree.root) > COMPLEX_TREE_MAX_DEPTH
  );
}
