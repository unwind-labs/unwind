import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactFlow, {
  Background,
  BackgroundVariant,
  Controls,
  type Edge,
  type Node,
  type NodeTypes,
  ReactFlowProvider,
  useReactFlow,
} from "reactflow";
import "reactflow/dist/style.css";
import { X } from "lucide-react";
import { useUi } from "@/store/ui";
import { isTypingTarget } from "@/lib/keyboard";
import { navigate } from "@/lib/url-sync";
import {
  COMPACT_CARD_WIDTH,
  CompactCardNode,
  type CompactCardData,
  deriveRows,
} from "@/panes/CompactCard";
import { ElbowEdge } from "@/panes/ElbowEdge";
import { TracePane } from "@/panes/TracePane";
import { useCanvasTree, useMessages } from "@/api/client";
import type { WindowNode } from "@/api/types";

const nodeTypes: NodeTypes = { compact: CompactCardNode };
const edgeTypes = { elbow: ElbowEdge };

/** Top-level canvas pane: lays out a tree of compact session cards. */
export function CanvasPane() {
  const slug = useUi((s) => s.slug);
  const rootSessionId = useUi((s) => s.rootSessionId);
  const detailSessionId = useUi((s) => s.detailSessionId);
  const detailWindow = useUi((s) => s.detailWindow);

  // ESC closes the detail overlay. Hook MUST come before any early returns
  // to keep the hook count stable across renders (React #310).
  useEffect(() => {
    if (!detailSessionId) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        navigate.closeDetail();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [detailSessionId]);

  if (!rootSessionId) {
    return (
      <Shell>
        <div className="flex h-full items-center justify-center text-xs text-muted-foreground">
          select a session on the left.
        </div>
      </Shell>
    );
  }

  // Render the canvas always — never unmount it when the detail overlay
  // opens, otherwise CanvasInner's local state (measured heights, user-
  // interacted ref, focused-node) all reset on close.
  return (
    <Shell>
      <div className="relative h-full w-full">
        <ReactFlowProvider>
          <CanvasInner
            slug={slug!}
            rootSessionId={rootSessionId}
            onOpenDetail={navigate.openDetail}
          />
        </ReactFlowProvider>
        {detailSessionId ? (
          <div className="absolute inset-0 z-10 flex flex-col bg-background">
            <div className="flex items-center justify-between gap-2 border-b border-border bg-card/60 px-3 py-1.5">
              <button
                type="button"
                onClick={() => navigate.closeDetail()}
                className="flex items-center gap-1 rounded px-2 py-0.5 text-[11px] text-muted-foreground hover:bg-accent/50 hover:text-foreground"
              >
                <X className="h-3 w-3" />
                back to canvas
              </button>
              <span className="text-[10px] text-muted-foreground">
                press <kbd className="rounded border border-border bg-muted px-1">esc</kbd> to close
              </span>
            </div>
            <div className="flex-1 overflow-hidden">
              <DetailView
                sessionId={detailSessionId}
                window={detailWindow}
              />
            </div>
          </div>
        ) : null}
      </div>
    </Shell>
  );
}

function Shell({ children }: { children: React.ReactNode }) {
  return <div className="flex h-full flex-col">{children}</div>;
}

/** Shows the TracePane for a given session_id without touching the store's
 *  rootSessionId (which would close this overlay and re-fit the canvas).
 *  When ``window`` is set, the trace is filtered to that ``[start, end)``
 *  range — used when the overlay was opened from a windowed canvas node. */
function DetailView({
  sessionId,
  window,
}: {
  sessionId: string;
  window: { start: string | null; end: string | null } | null;
}) {
  return <TracePane sessionIdOverride={sessionId} windowOverride={window} />;
}

// --- canvas inner -----------------------------------------------------------

type OpenDetailFn = (
  id: string,
  window?: { start: string | null; end: string | null } | null,
) => void;

function CanvasInner({
  slug,
  rootSessionId,
  onOpenDetail,
}: {
  slug: string;
  rootSessionId: string;
  onOpenDetail: OpenDetailFn;
}) {
  const detailSessionId = useUi((s) => s.detailSessionId);
  const focusedPane = useUi((s) => s.focusedPane);
  const rotateFocus = useUi((s) => s.rotateFocus);

  // Keyboard cursor on the canvas — a *node id* (window_id), distinct
  // from detailSessionId (which is a session id and means the overlay
  // is open). Up/Down move it, Enter opens detail. Lifted into the store
  // so url-sync can mirror it into the URL via replaceState (see
  // lib/url-sync's navigate.setCanvasFocus). The store's
  // ``selectRootSession`` action already clears focus when the root
  // changes, so no separate reset effect is needed — and removing it is
  // what lets a deep link like ``?session=Y&focus=A`` actually restore
  // the cursor on first mount.
  const canvasFocusedNodeId = useUi((s) => s.canvasFocusedNodeId);
  const setCanvasFocusedNodeId = useCallback(
    (id: string | null) => navigate.setCanvasFocus(id),
    [],
  );

  // Per-node measured heights — fed back from each card's ResizeObserver.
  const [measuredHeights, setMeasuredHeights] = useState<Record<string, number>>(
    {},
  );
  useEffect(() => {
    setMeasuredHeights({});
  }, [rootSessionId]);
  const handleMeasure = useCallback((id: string, h: number) => {
    setMeasuredHeights((prev) => {
      if (Math.abs((prev[id] ?? 0) - h) < 4) return prev;
      return { ...prev, [id]: h };
    });
  }, []);

  // The canvas tree — single source of truth. Backend computes it from
  // session JSONLs + callstack reports in one deterministic pass; we
  // just render the result.
  const { data: canvasTree } = useCanvasTree(slug, rootSessionId);

  // Auto-open the trace overlay when the root has no children (single-
  // card canvas would be a wasted view). We only do this once per root,
  // and only after the tree query has settled — without that gate, the
  // first render of an empty tree would auto-open and then the real
  // tree would arrive a beat later, producing a jarring open-then-close.
  const { data: rootMessages, isFetching: rootFetching } = useMessages(
    slug,
    rootSessionId,
    false,
  );
  const autoOpenedForRef = useRef<string | null>(null);
  useEffect(() => {
    if (!canvasTree) return;
    if (!rootMessages || rootFetching) return;
    if (autoOpenedForRef.current === rootSessionId) return;
    if (canvasTree.all_windows.length > 1) {
      autoOpenedForRef.current = rootSessionId;
      return;
    }
    const rows = deriveRows(
      rootMessages.messages,
      rootMessages.extra_spawns ?? [],
      rootMessages.messages,
    );
    if (rows.some((r) => r.kind === "spawn")) {
      // The tree query says one window but the message stream knows
      // about a spawn — wait for the next tree refresh rather than
      // auto-opening too eagerly.
      return;
    }
    const t = window.setTimeout(() => {
      if (canvasTree.all_windows.length > 1) {
        autoOpenedForRef.current = rootSessionId;
        return;
      }
      autoOpenedForRef.current = rootSessionId;
      // Auto-open is not a user nav: replaceState, no history entry.
      navigate.openDetailAuto(rootSessionId);
    }, 400);
    return () => window.clearTimeout(t);
  }, [
    canvasTree,
    rootMessages,
    rootFetching,
    rootSessionId,
    onOpenDetail,
  ]);

  // Forward declare focusAndPan so treeToReactFlow can reference it; we
  // assign the real implementation below (it depends on `nodes`).
  const focusAndPanRef = useRef<((id: string) => void) | null>(null);
  const handleFocusChild = useCallback((windowId: string) => {
    focusAndPanRef.current?.(windowId);
  }, []);

  // Build ReactFlow nodes + edges from the tree.
  const { nodes, edges } = useMemo(() => {
    if (!canvasTree) return { nodes: [], edges: [] };
    return treeToReactFlow({
      slug,
      tree: canvasTree.root,
      allWindows: canvasTree.all_windows,
      heights: measuredHeights,
      selectedSessionId: detailSessionId,
      keyboardFocusedNodeId: canvasFocusedNodeId,
      onOpenDetail,
      onFocusChild: handleFocusChild,
      onMeasure: handleMeasure,
    });
  }, [
    slug,
    canvasTree,
    measuredHeights,
    detailSessionId,
    canvasFocusedNodeId,
    onOpenDetail,
    handleFocusChild,
    handleMeasure,
  ]);

  // Track whether the user has panned/zoomed so AutoFit can defer to them.
  const userInteractedRef = useRef(false);
  const programmaticMoveRef = useRef(false);
  useEffect(() => {
    userInteractedRef.current = false;
  }, [rootSessionId]);

  // Compact signature of measured heights so AutoFit re-runs once real
  // sizes settle (cards initially render at default height; ResizeObserver
  // feeds back the real size).
  const heightsSignature = useMemo(() => {
    const ids = Object.keys(measuredHeights).sort();
    return ids.map((id) => `${id}:${Math.round(measuredHeights[id])}`).join("|");
  }, [measuredHeights]);

  // Keyboard navigation: ordered nodes (left→right column, then top→bottom),
  // and parent/children maps for ←/→ traversal.
  const orderedNodeIds = useMemo(() => {
    return nodes
      .slice()
      .sort((a, b) => a.position.x - b.position.x || a.position.y - b.position.y)
      .map((n) => n.id);
  }, [nodes]);

  const { parentByNode, childrenByNode } = useMemo(() => {
    const parent: Record<string, string> = {};
    const children: Record<string, string[]> = {};
    for (const e of edges) {
      parent[e.target] = e.source;
      (children[e.source] ??= []).push(e.target);
    }
    const yOf = (id: string) =>
      nodes.find((n) => n.id === id)?.position.y ?? 0;
    for (const k in children) {
      children[k].sort((a, b) => yOf(a) - yOf(b));
    }
    return { parentByNode: parent, childrenByNode: children };
  }, [edges, nodes]);

  const reactFlow = useReactFlow();

  // Pan + zoom helper, extracted so both the keydown handler and the
  // auto-focus effect (below) can reuse it.
  const FOCUS_ZOOM = 1;
  const focusAndPan = useCallback(
    (nextId: string) => {
      setCanvasFocusedNodeId(nextId);
      const node = nodes.find((n) => n.id === nextId);
      if (!node) return;
      const w = COMPACT_CARD_WIDTH;
      const h = measuredHeights[nextId] ?? DEFAULT_NODE_HEIGHT;
      const cx = node.position.x + w / 2;
      const cy = node.position.y + h / 2;
      const currentZoom = reactFlow.getZoom();
      const targetZoom = currentZoom < FOCUS_ZOOM ? FOCUS_ZOOM : currentZoom;
      programmaticMoveRef.current = true;
      reactFlow.setCenter(cx, cy, { zoom: targetZoom, duration: 200 });
      window.setTimeout(() => {
        programmaticMoveRef.current = false;
      }, 250);
    },
    [nodes, measuredHeights, reactFlow],
  );
  // Keep the ref in sync so the row-click closure (built before
  // focusAndPan exists in scope) calls the latest version.
  focusAndPanRef.current = focusAndPan;

  // When the user enters the canvas pane VIA KEYBOARD (←/→), default
  // the keyboard cursor to the root node — pan/zoom to it. Without
  // this, ↑/↓/←/→ from a fresh canvas have no anchor and feel
  // unresponsive. We deliberately do NOT auto-focus on mouse entry
  // (PaneFrame.onMouseDown) — clicking a specific node would race with
  // this effect and lose, leaving focus on the root instead of the
  // clicked node.
  const canvasEnterIntent = useUi((s) => s.canvasEnterIntent);
  const clearCanvasEnterIntent = useUi((s) => s.clearCanvasEnterIntent);
  useEffect(() => {
    if (canvasEnterIntent !== "keyboard") return;
    if (orderedNodeIds.length === 0) return;
    if (canvasFocusedNodeId === null) {
      focusAndPan(orderedNodeIds[0]);
    }
    clearCanvasEnterIntent();
  }, [
    canvasEnterIntent,
    canvasFocusedNodeId,
    orderedNodeIds,
    focusAndPan,
    clearCanvasEnterIntent,
  ]);

  const navStateRef = useRef({
    focusedPane,
    detailOpen: !!detailSessionId,
    orderedNodeIds,
    canvasFocusedNodeId,
    nodes,
    parentByNode,
    childrenByNode,
    rotateFocus,
    rootSessionId,
  });
  navStateRef.current = {
    focusedPane,
    detailOpen: !!detailSessionId,
    orderedNodeIds,
    canvasFocusedNodeId,
    nodes,
    parentByNode,
    childrenByNode,
    rotateFocus,
    rootSessionId,
  };

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (isTypingTarget(e)) return;
      const s = navStateRef.current;
      if (s.focusedPane !== "thread") return;
      if (s.detailOpen) return;

      const isUp = e.key === "ArrowUp" || e.key === "k";
      const isDown = e.key === "ArrowDown" || e.key === "j";
      const isLeft = e.key === "ArrowLeft" || e.key === "h";
      const isRight = e.key === "ArrowRight" || e.key === "l";
      const isEnter = e.key === "Enter";
      const isEsc = e.key === "Escape";
      if (!isUp && !isDown && !isLeft && !isRight && !isEnter && !isEsc) return;

      if (isEsc) {
        if (s.canvasFocusedNodeId) {
          e.preventDefault();
          setCanvasFocusedNodeId(null);
        }
        return;
      }

      if (isEnter) {
        if (s.canvasFocusedNodeId) {
          e.preventDefault();
          const focused = s.nodes.find((n) => n.id === s.canvasFocusedNodeId);
          const sid = focused?.data.sessionId ?? s.canvasFocusedNodeId;
          const win = focused
            ? { start: focused.data.windowStart, end: focused.data.windowEnd }
            : null;
          onOpenDetail(sid, win);
        }
        return;
      }

      if (isLeft || isRight) {
        e.preventDefault();
        const current =
          s.canvasFocusedNodeId ?? s.orderedNodeIds[0] ?? s.rootSessionId;
        if (isLeft) {
          const parent = s.parentByNode[current];
          if (parent) {
            focusAndPan(parent);
          } else {
            s.rotateFocus(-1);
          }
        } else {
          const kids = s.childrenByNode[current];
          if (kids && kids.length > 0) {
            focusAndPan(kids[0]);
          } else {
            // Leaf: hop to the next column.
            const cur = s.nodes.find((n) => n.id === current);
            if (cur) {
              const rightNodes = s.nodes.filter(
                (n) => n.position.x > cur.position.x,
              );
              if (rightNodes.length > 0) {
                const nextX = Math.min(...rightNodes.map((n) => n.position.x));
                const col = rightNodes.filter((n) => n.position.x === nextX);
                col.sort((a, b) => a.position.y - b.position.y);
                focusAndPan(col[0].id);
              }
            }
          }
        }
        return;
      }

      if (s.orderedNodeIds.length === 0) return;
      e.preventDefault();
      const idx = s.canvasFocusedNodeId
        ? s.orderedNodeIds.indexOf(s.canvasFocusedNodeId)
        : -1;
      const next = isDown
        ? Math.min(s.orderedNodeIds.length - 1, idx < 0 ? 0 : idx + 1)
        : Math.max(0, idx < 0 ? 0 : idx - 1);
      focusAndPan(s.orderedNodeIds[next]);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onOpenDetail, reactFlow, measuredHeights]);

  return (
    <div className="h-full w-full">
      <ReactFlow
        // Force a fresh ReactFlow mount on every session switch. Without
        // this, the internal nodeInternals map carries over from the prior
        // session — when the new tree's nodes are pushed in via the props,
        // ReactFlow's reconciliation sometimes fails to mount the new
        // cards visually.
        key={rootSessionId}
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        onNodeClick={(_, n) => {
          setCanvasFocusedNodeId(n.id);
          const d = n.data as CompactCardData | undefined;
          const sid = d?.sessionId ?? n.id;
          const win = d ? { start: d.windowStart, end: d.windowEnd } : null;
          onOpenDetail(sid, win);
        }}
        fitView
        fitViewOptions={{ padding: 0.2, maxZoom: 1.0, minZoom: 0.05 }}
        minZoom={0.05}
        maxZoom={1.5}
        proOptions={{ hideAttribution: true }}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
        onMoveStart={(e) => {
          if (programmaticMoveRef.current) return;
          if (e) userInteractedRef.current = true;
        }}
      >
        <Background variant={BackgroundVariant.Dots} gap={18} size={1} />
        <Controls showInteractive={false} />
        <AutoFit
          rootSessionId={rootSessionId}
          nodeCount={nodes.length}
          heightsSignature={heightsSignature}
          userInteracted={userInteractedRef}
          programmaticMove={programmaticMoveRef}
        />
      </ReactFlow>
    </div>
  );
}

/** Auto-fits the viewport to the current graph (debounced). */
function AutoFit({
  rootSessionId,
  nodeCount,
  heightsSignature,
  userInteracted,
  programmaticMove,
}: {
  rootSessionId: string;
  nodeCount: number;
  heightsSignature: string;
  userInteracted: React.MutableRefObject<boolean>;
  programmaticMove: React.MutableRefObject<boolean>;
}) {
  const { fitView } = useReactFlow();
  const lastCount = useRef(0);
  const lastSig = useRef("");
  const lastRoot = useRef<string | null>(null);
  const timerRef = useRef<number | null>(null);

  useEffect(() => {
    const rootChanged = lastRoot.current !== rootSessionId;
    const grew = nodeCount > lastCount.current;
    const heightsChanged = heightsSignature !== lastSig.current;

    lastRoot.current = rootSessionId;
    lastCount.current = nodeCount;
    lastSig.current = heightsSignature;

    if (!rootChanged && userInteracted.current) return;
    if (!rootChanged && !grew && !heightsChanged) return;

    if (timerRef.current != null) window.clearTimeout(timerRef.current);
    timerRef.current = window.setTimeout(() => {
      timerRef.current = null;
      programmaticMove.current = true;
      fitView({ padding: 0.2, maxZoom: 1.0, minZoom: 0.05, duration: 200 });
      window.setTimeout(() => {
        programmaticMove.current = false;
      }, 250);
    }, 150);

    return () => {
      if (timerRef.current != null) {
        window.clearTimeout(timerRef.current);
        timerRef.current = null;
      }
    };
  }, [
    rootSessionId,
    nodeCount,
    heightsSignature,
    fitView,
    userInteracted,
    programmaticMove,
  ]);
  return null;
}

// --- tree → ReactFlow nodes & edges ----------------------------------------

const DEFAULT_NODE_HEIGHT = 220;
const MIN_COLUMN_GAP = 80;
const SIBLING_GAP = 24;
// Per-edge horizontal stripe spacing, mirrored in ``treeToReactFlow``.
// Kept in sync so layoutTree can size each inter-column gap to fit the
// edge bundle leaving the wider parent at that depth.
const EDGE_OFFSET_STEP = 6;
const EDGE_BUNDLE_PADDING = 32;

/** Compute (x, y) for every WindowNode in the tree.
 *
 *  Children stack vertically below their parent's top edge. Each
 *  inter-column gap widens to fit the edge bundle leaving the widest
 *  parent at the source depth — so a parent with 14 siblings doesn't
 *  smush its 14 elbow stripes into the standard 80px gap.
 *  Subtree heights are computed bottom-up so siblings don't overlap.
 */
function layoutTree(
  root: WindowNode,
  heights: Record<string, number>,
): Record<string, { x: number; y: number }> {
  const positions: Record<string, { x: number; y: number }> = {};
  const heightOf = (n: WindowNode) =>
    heights[n.window_id] ?? DEFAULT_NODE_HEIGHT;

  // First pass: largest fan-out per depth, so we know how wide each
  // inter-column gap needs to be. Depth d's "fan-out" = max number of
  // direct children among parents at depth d.
  const maxFanOutAtDepth: number[] = [];
  function recordFanOut(n: WindowNode, depth: number) {
    maxFanOutAtDepth[depth] = Math.max(
      maxFanOutAtDepth[depth] ?? 0,
      n.children.length,
    );
    n.children.forEach((c) => recordFanOut(c, depth + 1));
  }
  recordFanOut(root, 0);

  const xOfDepth: number[] = [0];
  for (let d = 1; d <= maxFanOutAtDepth.length; d++) {
    const fanOut = maxFanOutAtDepth[d - 1] ?? 0;
    // Bundle width = (N-1) * step on each side of center, total ~= N * step.
    // Add padding so the outermost stripe still has breathing room
    // before each card edge.
    const gap =
      fanOut <= 1
        ? MIN_COLUMN_GAP
        : Math.max(MIN_COLUMN_GAP, fanOut * EDGE_OFFSET_STEP + EDGE_BUNDLE_PADDING);
    xOfDepth[d] = xOfDepth[d - 1] + COMPACT_CARD_WIDTH + gap;
  }

  const subtreeH: Record<string, number> = {};
  function computeH(n: WindowNode): number {
    let sum = 0;
    n.children.forEach((c, i) => {
      const ch = computeH(c);
      sum += ch;
      if (i > 0) sum += SIBLING_GAP;
    });
    const h = Math.max(heightOf(n), sum);
    subtreeH[n.window_id] = h;
    return h;
  }
  computeH(root);

  function place(n: WindowNode, depth: number, centerY: number) {
    positions[n.window_id] = {
      x: xOfDepth[depth] ?? depth * (COMPACT_CARD_WIDTH + MIN_COLUMN_GAP),
      y: centerY,
    };
    if (n.children.length === 0) return;
    // Top-align children to the parent's top edge so the FIRST child sits
    // visually next to the first call row in the parent card.
    const parentTop = centerY - heightOf(n) / 2;
    let cursorTop = parentTop;
    for (const c of n.children) {
      const ch = heightOf(c);
      place(c, depth + 1, cursorTop + ch / 2);
      cursorTop += subtreeH[c.window_id] + SIBLING_GAP;
    }
  }
  place(root, 0, 0);
  return positions;
}

function treeToReactFlow(args: {
  slug: string;
  tree: WindowNode;
  allWindows: WindowNode[];
  heights: Record<string, number>;
  selectedSessionId: string | null;
  keyboardFocusedNodeId: string | null;
  onOpenDetail: (id: string) => void;
  onFocusChild: (windowId: string) => void;
  onMeasure: (id: string, h: number) => void;
}): { nodes: Node<CompactCardData>[]; edges: Edge[] } {
  const {
    slug,
    tree,
    allWindows,
    heights,
    selectedSessionId,
    keyboardFocusedNodeId,
    onOpenDetail,
    onFocusChild,
    onMeasure,
  } = args;

  const positions = layoutTree(tree, heights);

  // Group child windows by their parent — sorted top-to-bottom by the
  // child's on-canvas Y. Used both for assigning per-parent edge
  // offsets (so vertical legs don't stack) and for telling each
  // CompactCard which Handles to render (one per child window_id).
  const childrenByParent: Record<string, WindowNode[]> = {};
  for (const w of allWindows) {
    if (!w.parent_window_id) continue;
    (childrenByParent[w.parent_window_id] ??= []).push(w);
  }
  for (const ws of Object.values(childrenByParent)) {
    ws.sort(
      (a, b) =>
        (positions[a.window_id]?.y ?? 0) - (positions[b.window_id]?.y ?? 0),
    );
  }

  // Build edges with per-parent symmetric offsets (e.g. 5 edges →
  // +12, +6, 0, -6, -12) so each vertical leg sits in its own
  // horizontal stripe. Topmost child → most positive offset.
  // ``EDGE_OFFSET_STEP`` is shared with ``layoutTree`` so the
  // inter-column gap widens to fit the bundle.
  const edges: Edge[] = [];
  for (const [parentId, ws] of Object.entries(childrenByParent)) {
    const N = ws.length;
    ws.forEach((w, i) => {
      const centerOffset = ((N - 1) / 2 - i) * EDGE_OFFSET_STEP;
      edges.push({
        id: `${parentId}::${w.window_id}`,
        source: parentId,
        target: w.window_id,
        sourceHandle: w.window_id,
        targetHandle: "in",
        type: "elbow",
        data: { offset: centerOffset },
        className:
          (w.kind === "subagent" ? "uw-edge-subagent" : "uw-edge-call") +
          (w.status === "live" || w.status === "yield"
            ? " uw-edge-pending"
            : ""),
      });
    });
  }

  // Build nodes.
  const nodes: Node<CompactCardData>[] = allWindows
    .filter((w) => positions[w.window_id])
    .map((w) => {
      const pos = positions[w.window_id];
      const h = heights[w.window_id] ?? DEFAULT_NODE_HEIGHT;
      const data: CompactCardData = {
        slug,
        sessionId: w.session_id,
        label: w.label,
        isRoot: w.kind === "root",
        selected: w.session_id === selectedSessionId,
        keyboardFocused:
          w.window_id === keyboardFocusedNodeId &&
          w.session_id !== selectedSessionId,
        onOpenDetail,
        onFocusChild,
        onMeasure,
        status: w.status === "yield" ? "yield" : w.status === "live" ? "live" : "done",
        nodeId: w.window_id,
        windowStart: w.window_start,
        windowEnd: w.window_end,
        isResumeInstance: w.kind === "resume",
        spawnKind: w.kind === "subagent" ? "subagent" : w.kind === "resume" ? "call" : (w.kind === "call" ? "call" : null),
        canvasChildren: (childrenByParent[w.window_id] ?? []).map((c) => ({
          window_id: c.window_id,
          session_id: c.session_id,
        })),
      };
      return {
        id: w.window_id,
        type: "compact",
        position: {
          x: pos.x - COMPACT_CARD_WIDTH / 2,
          y: pos.y - h / 2,
        },
        data,
        draggable: false,
      };
    });

  return { nodes, edges };
}
