import { describe, it, expect } from "vitest";
import {
  COMPLEX_TREE_NODE_COUNT,
  flattenTree,
  isComplexTree,
  leftAction,
  maxDepth,
  nextRowId,
  rightAction,
} from "./tree-nav";
import type { CanvasTreeResponse, WindowNode } from "@/api/types";

const ZERO = { cw: 0, cr: 0, r: 0, w: 0 };

function node(id: string, children: WindowNode[] = []): WindowNode {
  return {
    window_id: id,
    session_id: id,
    label: id,
    window_start: null,
    window_end: null,
    status: "done",
    subtree_status: "done",
    kind: "call",
    parent_window_id: null,
    window_index: 0,
    self_usage: ZERO,
    subtree_usage: ZERO,
    self_cost: ZERO,
    subtree_cost: ZERO,
    follower_edges: [],
    children,
  };
}

/** root ─ a ─ a1
 *        │   └ a2
 *        └ b
 *  The fixture every nav test shares so row order/depth assumptions stay
 *  in one place. */
const root = node("root", [node("a", [node("a1"), node("a2")]), node("b")]);

const ids = (rows: ReturnType<typeof flattenTree>) => rows.map((r) => r.node.window_id);

describe("flattenTree", () => {
  it("fully expands in pre-order with correct depths", () => {
    const rows = flattenTree(root, new Set());
    expect(ids(rows)).toEqual(["root", "a", "a1", "a2", "b"]);
    expect(rows.map((r) => r.depth)).toEqual([0, 1, 2, 2, 1]);
  });

  it("hides a collapsed node's descendants but keeps the node itself visible", () => {
    // A user collapsing 'a' must still SEE 'a' (to re-expand it) — only its
    // subtree disappears. Regression guard: dropping the node entirely would
    // strand the collapse toggle.
    const rows = flattenTree(root, new Set(["a"]));
    expect(ids(rows)).toEqual(["root", "a", "b"]);
    expect(rows.find((r) => r.node.window_id === "a")?.collapsed).toBe(true);
  });
});

describe("nextRowId", () => {
  const rows = flattenTree(root, new Set());

  it("moves down and up through visible rows", () => {
    expect(nextRowId(rows, "root", 1)).toBe("a");
    expect(nextRowId(rows, "a2", 1)).toBe("b");
    expect(nextRowId(rows, "a", -1)).toBe("root");
  });

  it("clamps at both ends instead of wrapping", () => {
    expect(nextRowId(rows, "root", -1)).toBe("root");
    expect(nextRowId(rows, "b", 1)).toBe("b");
  });

  it("lands on the first row when entering with no focus", () => {
    expect(nextRowId(rows, null, 1)).toBe("root");
  });
});

describe("rightAction", () => {
  it("does nothing on a leaf", () => {
    const rows = flattenTree(root, new Set());
    expect(rightAction(rows, "a1", new Set())).toEqual({ type: "none" });
  });

  it("expands a collapsed parent", () => {
    const collapsed = new Set(["a"]);
    const rows = flattenTree(root, collapsed);
    expect(rightAction(rows, "a", collapsed)).toEqual({ type: "expand", id: "a" });
  });

  it("dives into the first child of an already-expanded parent", () => {
    const rows = flattenTree(root, new Set());
    expect(rightAction(rows, "a", new Set())).toEqual({ type: "focus", id: "a1" });
  });
});

describe("leftAction", () => {
  it("collapses an expanded parent", () => {
    const rows = flattenTree(root, new Set());
    expect(leftAction(rows, "a", new Set())).toEqual({ type: "collapse", id: "a" });
  });

  it("steps out to the parent from a leaf", () => {
    const rows = flattenTree(root, new Set());
    expect(leftAction(rows, "a1", new Set())).toEqual({ type: "focus", id: "a" });
  });

  it("steps out to the parent from a collapsed node", () => {
    const collapsed = new Set(["a"]);
    const rows = flattenTree(root, collapsed);
    expect(leftAction(rows, "a", collapsed)).toEqual({ type: "focus", id: "root" });
  });

  it("reports nowhere-to-go at the root", () => {
    // The pane turns this into "hand focus back to the session list".
    const rows = flattenTree(root, new Set());
    expect(leftAction(rows, "root", new Set())).toEqual({ type: "none" });
  });
});

describe("isComplexTree", () => {
  const tree = (root: WindowNode, all: WindowNode[]): CanvasTreeResponse => ({
    root,
    all_windows: all,
  });

  it("is false for a small, shallow run", () => {
    const all = [root, ...root.children, ...root.children[0].children];
    expect(isComplexTree(tree(root, all))).toBe(false);
  });

  it("is true once the rendered tree has many nodes", () => {
    const many = Array.from({ length: COMPLEX_TREE_NODE_COUNT + 1 }, (_, i) => node(`n${i}`));
    const wide = node("root", many);
    expect(isComplexTree(tree(wide, [wide, ...many]))).toBe(true);
  });

  it("is true for a deep call chain even with few windows", () => {
    // c0 → c1 → … → c5 is only 6 windows but 5 levels deep — the canvas
    // spreads that across 6 columns, which is exactly when text view wins.
    let chain = node("c5");
    for (let i = 4; i >= 0; i--) chain = node(`c${i}`, [chain]);
    expect(maxDepth(chain)).toBe(5);
    expect(isComplexTree(tree(chain, [chain]))).toBe(true);
  });

  it("ignores orphan windows that the views never render", () => {
    // Regression: ``all_windows`` can carry hundreds of orphan windows
    // (parent=null, unreachable from root) that the canvas filters out. A
    // tiny REACHABLE tree must stay "simple" — judging by all_windows.length
    // would wrongly force the text view onto a 2-card run.
    const small = node("root", [node("a")]);
    const orphans = Array.from({ length: 500 }, (_, i) => node(`orphan${i}`));
    expect(isComplexTree(tree(small, [small, small.children[0], ...orphans]))).toBe(false);
  });
});
