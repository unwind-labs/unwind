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
import {
  COMPACT_CARD_WIDTH,
  CompactCardNode,
  type CompactCardData,
  deriveRows,
} from "@/panes/CompactCard";
import { ElbowEdge } from "@/panes/ElbowEdge";
import { TracePane } from "@/panes/TracePane";
import { useMessages, useSessions } from "@/api/client";
import { windowsForParent, type SpawnEdgeInfo } from "@/panes/instances";

const nodeTypes: NodeTypes = { compact: CompactCardNode };
const edgeTypes = { elbow: ElbowEdge };

/** Top-level canvas pane: lays out a tree of compact session cards. */
export function CanvasPane() {
  const slug = useUi((s) => s.slug);
  const rootSessionId = useUi((s) => s.rootSessionId);
  const detailSessionId = useUi((s) => s.detailSessionId);
  const detailWindow = useUi((s) => s.detailWindow);
  const openDetail = useUi((s) => s.openDetail);
  const closeDetail = useUi((s) => s.closeDetail);

  // ESC closes the detail overlay. Hook MUST come before any early returns
  // to keep the hook count stable across renders (React #310).
  useEffect(() => {
    if (!detailSessionId) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        closeDetail();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [detailSessionId, closeDetail]);

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
  // opens, otherwise CanvasInner's local state (knownIds, spawnsByParent,
  // labels, FitOnGrowth's "last seen count" ref, user-interacted ref) all
  // reset on close, causing a refit and apparent loss of nodes.
  return (
    <Shell>
      <div className="relative h-full w-full">
        <ReactFlowProvider>
          <CanvasInner
            slug={slug!}
            rootSessionId={rootSessionId}
            onOpenDetail={openDetail}
          />
        </ReactFlowProvider>
        {detailSessionId ? (
          <div className="absolute inset-0 z-10 flex flex-col bg-background">
            <div className="flex items-center justify-between gap-2 border-b border-border bg-card/60 px-3 py-1.5">
              <button
                type="button"
                onClick={closeDetail}
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
  // Keyboard cursor on the canvas — a *node id*, distinct from detailSessionId
  // (which is a session id and means the overlay is open). Up/Down move it,
  // Enter opens detail (which routes by sessionId).
  const [canvasFocusedNodeId, setCanvasFocusedNodeId] = useState<string | null>(
    rootSessionId,
  );
  useEffect(() => {
    setCanvasFocusedNodeId(rootSessionId);
  }, [rootSessionId]);
  // Per-card spawn metadata, keyed by parent NODE id (not session id —
  // multiple cards can share a sessionId across invoke + invoke_resume; each
  // gets its own entry here keyed by its handle-id-as-nodeId).
  const [spawnsByParent, setSpawnsByParent] = useState<
    Record<string, SpawnEdgeInfo[]>
  >({});

  // Reset whenever the root changes.
  useEffect(() => {
    setSpawnsByParent({});
    // Drop stale measurements from the previous session so heightsSignature
    // isn't polluted with phantom entries (which would otherwise trigger
    // extra fits on the new session for nodes that aren't on the canvas).
    setMeasuredHeights({});
  }, [rootSessionId]);

  // If the selected session has no children at all (no callstack forks, no
  // subagents), the canvas would just show one card on its own. In that case,
  // jump straight into the session's full trace view — the user can still
  // press ESC to return to the (single-card) canvas.
  //
  // We have to be careful here: useMessages can return cached/partial data
  // before all children have been discovered, and `rows.some(spawn)` can
  // briefly read false for a session that actually has children. If we
  // auto-opened on that first reading, the user would see a flash of the
  // canvas and then the trace overlay would hide it — exactly the
  // "momentary render and then it goes away" symptom. To avoid that:
  //   1. Wait until the messages query is settled (not fetching).
  //   2. Wait an additional settle window. If a child is discovered during
  //      the window (knownIds grows past 1), cancel the auto-open and mark
  //      the decision as "no, has children".
  const { data: rootMessages, isFetching: rootFetching } = useMessages(
    slug,
    rootSessionId,
    false,
  );
  // Number of known node ids — used as a "has children" signal for auto-open.
  // We can't compute this from spawnsByParent alone before the canvas builds
  // its graph, so derive a quick lower bound: 1 (root) + total spawn entries
  // with a resolved childSessionId.
  const knownNodeCount = useMemo(() => {
    let n = 1;
    for (const list of Object.values(spawnsByParent)) {
      for (const sp of list) if (sp.childSessionId) n += 1;
    }
    return n;
  }, [spawnsByParent]);

  const autoOpenedForRef = useRef<string | null>(null);
  useEffect(() => {
    if (!rootMessages || rootFetching) return;
    if (autoOpenedForRef.current === rootSessionId) return;
    if (knownNodeCount > 1) {
      // Children already on the canvas — definitely not single-node.
      autoOpenedForRef.current = rootSessionId;
      return;
    }
    const rows = deriveRows(
      rootMessages.messages,
      rootMessages.extra_spawns ?? [],
    );
    const hasSpawns = rows.some((r) => r.kind === "spawn");
    if (hasSpawns) {
      // Spawns visible in messages but child cards not yet added (race with
      // CompactCard's onSpawnsResolved). Bail out; the deps will re-run this
      // effect when spawnsByParent grows.
      return;
    }
    const t = window.setTimeout(() => {
      if (knownNodeCount > 1) {
        autoOpenedForRef.current = rootSessionId;
        return;
      }
      autoOpenedForRef.current = rootSessionId;
      onOpenDetail(rootSessionId);
    }, 400);
    return () => window.clearTimeout(t);
  }, [
    rootMessages,
    rootFetching,
    rootSessionId,
    knownNodeCount,
    onOpenDetail,
  ]);

  // Pull session list to surface friendly titles for root.
  const { data: sessions } = useSessions(slug, true);
  const rootTitle = useMemo(() => {
    const row = sessions?.find((s) => s.session_id === rootSessionId);
    return row?.title ?? rootSessionId.slice(0, 8);
  }, [sessions, rootSessionId]);

  // Per-session status from the sessions API. Real Claude sessions
  // (non-subagent) are LIVE if a claude process is running for the project
  // OR the JSONL was touched in the last 5 minutes; DONE otherwise. The
  // backend computes this in processes.session_status.
  const apiStatuses = useMemo(() => {
    const out: Record<string, "live" | "yield" | "done"> = {};
    for (const s of sessions ?? []) {
      out[s.session_id] =
        s.status === "yield"
          ? "yield"
          : s.status === "live"
            ? "live"
            : "done";
    }
    return out;
  }, [sessions]);

  // Sessions in spawnsByParent that are LIVE (no result yet) — used as a
  // fallback for ids the sessions API doesn't know about (e.g. ``agent-<id>``
  // synthetic subagent ids). Keyed by SESSION id, not node id, since this
  // feeds the per-session status badge.
  const liveSessionIds = useMemo(() => {
    const out = new Set<string>();
    for (const list of Object.values(spawnsByParent)) {
      for (const sp of list) {
        if (!sp.done && sp.childSessionId) out.add(sp.childSessionId);
      }
    }
    return out;
  }, [spawnsByParent]);

  const handleSpawnsResolved = useCallback<CompactCardData["onSpawnsResolved"]>(
    (parentNodeId, spawns) => {
      setSpawnsByParent((prev) => {
        const incoming: SpawnEdgeInfo[] = spawns.map((s) => ({
          parent: parentNodeId,
          child: s.handleId,
          childSessionId: s.childId,
          handleId: s.handleId,
          spawnKind: s.spawnKind,
          done: s.done,
          label: s.label || s.childId.slice(0, 8),
          parentToolUseTs: s.parentToolUseTs,
          isResume: s.isResume,
          userReply: s.userReply,
        }));
        // MERGE rather than replace. callstack mid-flight can reorder or
        // temporarily lose entries (e.g., when a tool_use's claimed report
        // shifts as new reports are written). A pure replace would leave
        // any previously-known children as orphans on the canvas. We dedupe
        // by handleId — a stable per-(tool_use, child-index) key — so
        // status updates flow through (later entry wins) but we never lose
        // a child once observed.
        const existing = prev[parentNodeId] ?? [];
        const map = new Map<string, SpawnEdgeInfo>();
        for (const sp of existing) map.set(sp.handleId, sp);
        for (const sp of incoming) map.set(sp.handleId, sp);

        // Preserve order: incoming order first (current request order from
        // the parent's tool_use), then any leftover from existing not in
        // incoming (preserved in their original relative order).
        const incomingHandles = new Set(incoming.map((s) => s.handleId));
        const tail = existing.filter((s) => !incomingHandles.has(s.handleId));
        const ordered = [
          ...incoming.map((s) => map.get(s.handleId)!),
          ...tail.map((s) => map.get(s.handleId)!),
        ];

        // Skip update if nothing meaningful changed.
        const same =
          existing.length === ordered.length &&
          existing.every((b, i) => {
            const m = ordered[i];
            return (
              m &&
              b.childSessionId === m.childSessionId &&
              b.handleId === m.handleId &&
              b.done === m.done &&
              b.label === m.label &&
              b.parentToolUseTs === m.parentToolUseTs &&
              b.isResume === m.isResume
            );
          });
        if (same) return prev;
        return { ...prev, [parentNodeId]: ordered };
      });
    },
    [],
  );

  // Track measured heights per node, reported by each card via ResizeObserver
  // (in CompactCardNode → handleSpawnsResolved doesn't carry size, so we read
  // them via a separate handleMeasure callback).
  const [measuredHeights, setMeasuredHeights] = useState<Record<string, number>>(
    {},
  );
  const handleMeasure = useCallback((id: string, h: number) => {
    setMeasuredHeights((prev) => {
      if (Math.abs((prev[id] ?? 0) - h) < 4) return prev;
      return { ...prev, [id]: h };
    });
  }, []);

  // Per-session start timestamp, used to order siblings chronologically
  // top-to-bottom within each rank (depth column).
  const startTimes = useMemo(() => {
    const out: Record<string, number> = {};
    for (const s of sessions ?? []) {
      if (s.first_timestamp) out[s.session_id] = Date.parse(s.first_timestamp);
    }
    return out;
  }, [sessions]);

  const { nodes, edges } = useMemo(() => {
    return buildGraph({
      slug,
      rootSessionId,
      rootTitle,
      spawnsByParent,
      heights: measuredHeights,
      liveSessionIds,
      apiStatuses,
      startTimes,
      selectedSessionId: detailSessionId,
      keyboardFocusedNodeId: canvasFocusedNodeId,
      onSpawnsResolved: handleSpawnsResolved,
      onOpenDetail,
      onMeasure: handleMeasure,
    });
  }, [
    slug,
    rootSessionId,
    rootTitle,
    spawnsByParent,
    measuredHeights,
    liveSessionIds,
    apiStatuses,
    startTimes,
    detailSessionId,
    canvasFocusedNodeId,
    handleSpawnsResolved,
    onOpenDetail,
    handleMeasure,
  ]);

  // Track whether the user has panned/zoomed. Once they have, we stop
  // auto-fitting on every node addition. Reset on session switch so the
  // fresh tree gets framed properly.
  const userInteractedRef = useRef(false);
  // Programmatic-move guard. ReactFlow's onMoveStart fires for our OWN
  // fitView/setCenter calls (the `event` arg is non-null for those too in
  // some versions), so without this flag the very first auto-fit would
  // mark `userInteractedRef = true` and lock out every subsequent auto-fit
  // — leaving newly-discovered child nodes off-screen indefinitely.
  const programmaticMoveRef = useRef(false);
  useEffect(() => {
    userInteractedRef.current = false;
  }, [rootSessionId]);

  // Compact signature of all measured heights so the auto-fit re-runs once
  // the actual rendered sizes settle (cards initially render at default
  // height, then ResizeObserver feeds back the real size — without this,
  // fitView would frame the estimated layout and miss bottom rows).
  const heightsSignature = useMemo(() => {
    const ids = Object.keys(measuredHeights).sort();
    return ids.map((id) => `${id}:${Math.round(measuredHeights[id])}`).join("|");
  }, [measuredHeights]);

  // Sort visible nodes by position (column, then top-to-bottom within
  // column) so arrow-key navigation walks the tree in a stable, visually
  // intuitive order. Memoize on `nodes` only — we want to recompute when
  // layout changes, not on every keystroke.
  const orderedNodeIds = useMemo(() => {
    return nodes
      .slice()
      .sort((a, b) => a.position.x - b.position.x || a.position.y - b.position.y)
      .map((n) => n.id);
  }, [nodes]);

  // Latest values for the keydown handler — avoids re-attaching the
  // listener on every keystroke / state tick.
  const reactFlow = useReactFlow();
  const navStateRef = useRef({
    focusedPane,
    detailOpen: !!detailSessionId,
    orderedNodeIds,
    canvasFocusedNodeId,
    nodes,
  });
  navStateRef.current = {
    focusedPane,
    detailOpen: !!detailSessionId,
    orderedNodeIds,
    canvasFocusedNodeId,
    nodes,
  };

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      const typing =
        target &&
        (target.tagName === "INPUT" ||
          target.tagName === "TEXTAREA" ||
          target.isContentEditable);
      if (typing) return;
      const s = navStateRef.current;
      if (s.focusedPane !== "thread") return;
      if (s.detailOpen) return; // ESC handler owns the overlay's keys

      const isUp = e.key === "ArrowUp" || e.key === "k";
      const isDown = e.key === "ArrowDown" || e.key === "j";
      const isEnter = e.key === "Enter";
      const isEsc = e.key === "Escape";
      if (!isUp && !isDown && !isEnter && !isEsc) return;

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
          // Map nodeId → (sessionId, window). The overlay is keyed by
          // session id; the window narrows the trace to this slice.
          const focused = s.nodes.find(
            (n) => n.id === s.canvasFocusedNodeId,
          );
          const sid = focused?.data.sessionId ?? s.canvasFocusedNodeId;
          const win = focused
            ? {
                start: focused.data.windowStart,
                end: focused.data.windowEnd,
              }
            : null;
          onOpenDetail(sid, win);
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
      const nextId = s.orderedNodeIds[next];
      setCanvasFocusedNodeId(nextId);

      // Pan so the newly-focused node stays on screen. Use the node's
      // measured center; setCenter respects the current zoom.
      const node = s.nodes.find((n) => n.id === nextId);
      if (node) {
        const w = COMPACT_CARD_WIDTH;
        const h = measuredHeights[nextId] ?? DEFAULT_NODE_HEIGHT;
        const cx = node.position.x + w / 2;
        const cy = node.position.y + h / 2;
        programmaticMoveRef.current = true;
        reactFlow.setCenter(cx, cy, {
          zoom: reactFlow.getZoom(),
          duration: 200,
        });
        window.setTimeout(() => {
          programmaticMoveRef.current = false;
        }, 250);
      }
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
        // cards visually (you can pan to where they should be and see a
        // blank grid). A keyed remount throws all that internal state
        // away and starts clean.
        key={rootSessionId}
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        onNodeClick={(_, n) => {
          setCanvasFocusedNodeId(n.id);
          // n.id is a node id (handle id for child instances); the overlay
          // is keyed by sessionId. We also pass the window so the trace
          // shows only this slice of the session.
          const d = n.data as CompactCardData | undefined;
          const sid = d?.sessionId ?? n.id;
          const win = d
            ? { start: d.windowStart, end: d.windowEnd }
            : null;
          onOpenDetail(sid, win);
        }}
        fitView
        fitViewOptions={{ padding: 0.2, maxZoom: 1.0, minZoom: 0.05 }}
        // minZoom is intentionally low: a deep fork-tree at 340px-wide
        // cards can need zoom <0.1 to fit; capping at 0.2 (the previous
        // default) made fitView bail out and only ~5 cards would land in
        // the viewport while the rest sat off-screen but reachable via
        // keyboard nav.
        minZoom={0.05}
        maxZoom={1.5}
        proOptions={{ hideAttribution: true }}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
        onMoveStart={(e) => {
          // ReactFlow's onMoveStart fires for programmatic moves too (and
          // sometimes WITH a non-null event arg), so the `if (e)` guard
          // alone isn't enough — without programmaticMoveRef the very
          // first auto-fit would lock us out of all future auto-fits and
          // newly-arriving child nodes would never get framed.
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

/** Auto-fits the viewport to the current graph.
 *
 *  Three triggers, each gated on `!userInteracted` so we never fight a user
 *  who's panned/zoomed:
 *    1. rootSessionId changes — force-fit immediately (ignore the user-
 *       interacted flag for this case since it was just reset).
 *    2. nodeCount grows — schedule a debounced fit so progressive child
 *       discovery keeps the whole tree in frame.
 *    3. heightsSignature changes — re-fit once real measured heights replace
 *       the initial estimates, otherwise fitView frames the wrong bbox and
 *       bottom cards get clipped.
 *
 *  All three funnel into a single trailing-debounced timer so a burst of
 *  changes (root switch + immediate measurements) collapses to one fit. */
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
    // Slightly longer delay than before — gives ResizeObserver time to feed
    // back actual heights for newly-rendered cards before we commit to a fit.
    timerRef.current = window.setTimeout(() => {
      timerRef.current = null;
      // Mark this as a programmatic move so onMoveStart doesn't flip
      // userInteracted to true. Window covers the 200ms animation.
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

// --- layout -----------------------------------------------------------------

const DEFAULT_NODE_HEIGHT = 220;
const COLUMN_GAP = 80;
const SIBLING_GAP = 24;

function layoutTree(
  rootNodeId: string,
  knownNodeIds: string[],
  spawnsByParent: Record<string, SpawnEdgeInfo[]>,
  heights: Record<string, number>,
  startTimes: Record<string, number>,
): Record<string, { x: number; y: number }> {
  const known = new Set(knownNodeIds);
  const positions: Record<string, { x: number; y: number }> = {};
  if (!known.has(rootNodeId)) return positions;

  // Track per-id startTime for tooltip / debugging only — layout uses spawn
  // order, NOT sortable timestamps.
  void startTimes;

  // Group a parent's children by their tool_use_id so all children spawned
  // from the same ``invoke_parallel`` stay visually grouped. handleId is
  // ``spawn-<toolUseId>-<i>`` — strip the trailing ``-<i>`` for the group
  // key. invoke / invoke_resume each have their own tool_use_id and so end
  // up in distinct groups, which is exactly what we want for chronological
  // stacking of resume instances.
  const childGroupsOf = (parentNodeId: string): string[][] => {
    const groups: Record<string, string[]> = {};
    const groupOrder: string[] = [];
    const seen = new Set<string>();
    for (const sp of spawnsByParent[parentNodeId] ?? []) {
      const childNodeId = sp.handleId;
      if (!known.has(childNodeId) || seen.has(childNodeId)) continue;
      seen.add(childNodeId);
      const groupKey = sp.handleId.replace(/-\d+$/, "");
      if (!groups[groupKey]) {
        groups[groupKey] = [];
        groupOrder.push(groupKey);
      }
      groups[groupKey].push(childNodeId);
    }
    return groupOrder.map((k) => groups[k]);
  };

  // Flat list of all children (across groups, dedup), used for subtree-size
  // calculation.
  const allChildrenOf = (parent: string): string[] => {
    const out: string[] = [];
    for (const g of childGroupsOf(parent)) out.push(...g);
    return out;
  };

  const heightOf = (id: string) => heights[id] ?? DEFAULT_NODE_HEIGHT;

  // Subtree height = max(node's own height, sum of children subtree heights + gaps).
  // Approximation — within a group children stack tightly; across groups we
  // don't add extra gap here since the per-group layout handles that. Good
  // enough for sibling spacing; the actual placement loop below enforces no
  // overlap regardless.
  const visited = new Set<string>();
  const subtreeH: Record<string, number> = {};
  function compute(id: string) {
    if (visited.has(id)) return;
    visited.add(id);
    const kids = allChildrenOf(id);
    let sum = 0;
    for (let i = 0; i < kids.length; i++) {
      compute(kids[i]);
      sum += subtreeH[kids[i]];
      if (i > 0) sum += SIBLING_GAP;
    }
    subtreeH[id] = Math.max(heightOf(id), sum);
  }
  compute(rootNodeId);

  // Place a node and recurse. Children are organized by CALL row groups:
  //   - Group 1's first child's TOP aligned to parent's TOP.
  //   - Subsequent children in group 1 stack below.
  //   - Group 2's first child also tries to align to parent's top, but
  //     if group 1 extends past that, push group 2 below group 1 (+gap).
  //   - Within group 2, children stack normally.
  // Top-alignment (rather than center-alignment) keeps the first CALL row
  // — which is at the TOP of the parent's spawn list — visually next to
  // its child instead of dipping down to a centered child.
  // Visited guard. ``place`` recurses on each child via ``childGroupsOf``,
  // and inherited tool_uses (subagents inherit their parent's tool_use
  // blocks) can produce an apparent cycle in spawnsByParent. Without this
  // guard the recursion blows the JS stack.
  const placed = new Set<string>();
  function place(id: string, depth: number, centerY: number) {
    if (placed.has(id)) return;
    placed.add(id);
    positions[id] = {
      x: depth * (COMPACT_CARD_WIDTH + COLUMN_GAP),
      y: centerY,
    };
    const groups = childGroupsOf(id);
    if (groups.length === 0) return;

    const parentTop = centerY - heightOf(id) / 2;
    let prevGroupBottom: number | null = null;
    for (const group of groups) {
      if (group.length === 0) continue;
      // Where the first child of this group WANTS to go: aligned to the
      // parent's top edge.
      const desiredFirstTop = parentTop;
      const minTop: number =
        prevGroupBottom == null
          ? desiredFirstTop
          : Math.max(desiredFirstTop, prevGroupBottom + SIBLING_GAP);
      const firstTop: number = minTop;
      const firstHeight = heightOf(group[0]);
      const firstCenter = firstTop + firstHeight / 2;
      place(group[0], depth + 1, firstCenter);
      let cursorTop: number = firstTop + subtreeH[group[0]] + SIBLING_GAP;
      for (let i = 1; i < group.length; i++) {
        const kid = group[i];
        const kidH = heightOf(kid);
        place(kid, depth + 1, cursorTop + kidH / 2);
        cursorTop += subtreeH[kid] + SIBLING_GAP;
      }
      prevGroupBottom = cursorTop - SIBLING_GAP;
    }
  }
  place(rootNodeId, 0, 0);

  // Detached nodes (not reachable from root through spawnsByParent yet) get
  // dropped into their own column at the bottom. Rare — only if our
  // discovery is partial.
  let detachedY = 0;
  for (const id of knownNodeIds) {
    if (positions[id]) {
      detachedY = Math.max(
        detachedY,
        positions[id].y + heightOf(id) / 2 + SIBLING_GAP,
      );
    }
  }
  for (const id of knownNodeIds) {
    if (positions[id]) continue;
    positions[id] = { x: 0, y: detachedY + heightOf(id) / 2 };
    detachedY += heightOf(id) + SIBLING_GAP;
  }
  return positions;
}

/** A canvas node IS a ``(sessionId, [windowStart, windowEnd))`` tuple — a
 *  Claude session viewed through a single time window. Root has both ends
 *  ``null`` (the whole session). ``windowEnd === null`` means open-ended
 *  ("still running / latest"). */
type CanvasNode = {
  nodeId: string;
  sessionId: string;
  parentNodeId: string | null;
  windowStart: string | null;
  windowEnd: string | null;
  isResumeInstance: boolean;
  spawnKind: "call" | "subagent" | null;
  done: boolean;
  label: string;
};

function buildCanvasNodes(
  rootSessionId: string,
  rootTitle: string,
  spawnsByParent: Record<string, SpawnEdgeInfo[]>,
): { nodeMap: Record<string, CanvasNode>; order: string[] } {
  const nodeMap: Record<string, CanvasNode> = {};
  const order: string[] = [];
  nodeMap[rootSessionId] = {
    nodeId: rootSessionId,
    sessionId: rootSessionId,
    parentNodeId: null,
    windowStart: null,
    windowEnd: null,
    isResumeInstance: false,
    spawnKind: null,
    done: false,
    label: rootTitle,
  };
  order.push(rootSessionId);
  // BFS so we process parents before children — child labels/windows depend
  // on their parent's spawnsByParent entry.
  const queue: string[] = [rootSessionId];
  const visited = new Set<string>([rootSessionId]);
  while (queue.length) {
    const parentNodeId = queue.shift()!;
    const spawns = spawnsByParent[parentNodeId];
    if (!spawns?.length) continue;
    const windows = windowsForParent(parentNodeId, spawns);
    // Index spawns by handleId for label lookup. Each window's ``nodeId``
    // IS its spawn's handleId.
    const byHandle = new Map<string, SpawnEdgeInfo>();
    for (const sp of spawns) byHandle.set(sp.handleId, sp);
    for (const win of windows) {
      if (visited.has(win.nodeId)) continue;
      visited.add(win.nodeId);
      const sp = byHandle.get(win.nodeId);
      const label =
        sp?.label ||
        (win.sessionId.startsWith("agent-")
          ? win.sessionId.slice(6, 14)
          : win.sessionId.slice(0, 8));
      nodeMap[win.nodeId] = {
        nodeId: win.nodeId,
        sessionId: win.sessionId,
        parentNodeId,
        windowStart: win.windowStart,
        windowEnd: win.windowEnd,
        isResumeInstance: win.isResume,
        spawnKind: win.spawnKind,
        done: win.done,
        label,
      };
      order.push(win.nodeId);
      queue.push(win.nodeId);
    }
  }
  return { nodeMap, order };
}

function buildGraph(args: {
  slug: string;
  rootSessionId: string;
  rootTitle: string;
  spawnsByParent: Record<string, SpawnEdgeInfo[]>;
  heights: Record<string, number>;
  liveSessionIds: Set<string>;
  apiStatuses: Record<string, "live" | "yield" | "done">;
  startTimes: Record<string, number>;
  selectedSessionId: string | null;
  keyboardFocusedNodeId: string | null;
  onSpawnsResolved: CompactCardData["onSpawnsResolved"];
  onOpenDetail: (id: string) => void;
  onMeasure: (id: string, h: number) => void;
}) {
  const {
    slug,
    rootSessionId,
    rootTitle,
    spawnsByParent,
    heights,
    liveSessionIds,
    apiStatuses,
    startTimes,
    selectedSessionId,
    keyboardFocusedNodeId,
    onSpawnsResolved,
    onOpenDetail,
    onMeasure,
  } = args;

  const { nodeMap, order: knownNodeIds } = buildCanvasNodes(
    rootSessionId,
    rootTitle,
    spawnsByParent,
  );

  const positions = layoutTree(
    rootSessionId,
    knownNodeIds,
    spawnsByParent,
    heights,
    startTimes,
  );

  // Offset edges per-parent so each call's vertical leg sits at a unique
  // horizontal stripe. Symmetric around the column midpoint: 3 edges → (+6,
  // 0, -6) px. Important for invoke + invoke_resume → distinct stripes
  // even though they target distinct nodes (helps when the resume node
  // sits close to the original).
  const EDGE_OFFSET_STEP = 6;
  const edges: Edge[] = [];
  for (const [parentId, spawns] of Object.entries(spawnsByParent)) {
    if (!nodeMap[parentId]) continue;
    const visible = spawns.filter((sp) => nodeMap[sp.handleId]);
    const N = visible.length;
    visible.forEach((sp, i) => {
      const centerOffset = ((N - 1) / 2 - i) * EDGE_OFFSET_STEP;
      edges.push({
        id: `${parentId}::${sp.handleId}`,
        source: parentId,
        target: sp.handleId,
        sourceHandle: sp.handleId,
        targetHandle: "in",
        type: "elbow",
        data: { offset: centerOffset },
        className:
          (sp.spawnKind === "call" ? "uw-edge-call" : "uw-edge-subagent") +
          (sp.done ? "" : " uw-edge-pending"),
      });
    });
  }

  const nodes: Node<CompactCardData>[] = knownNodeIds.map((nodeId) => {
    const cn = nodeMap[nodeId];
    const pos = positions[nodeId] ?? { x: 0, y: 0 };
    const h = heights[nodeId] ?? DEFAULT_NODE_HEIGHT;
    // Status priority for the canvas card:
    //   1. Bounded window (``windowEnd != null``) → done: this slice ended
    //      when a later resume started.
    //   2. sessions API (process detection + JSONL mtime fallback).
    //   3. spawn-row liveness — covers subagents (``agent-<id>``).
    const isLatest = cn.windowEnd === null;
    const status: "live" | "yield" | "done" = !isLatest
      ? "done"
      : (apiStatuses[cn.sessionId] ??
        (liveSessionIds.has(cn.sessionId) ? "live" : "done"));
    const data: CompactCardData = {
      slug,
      sessionId: cn.sessionId,
      label: cn.label,
      isRoot: nodeId === rootSessionId,
      selected: cn.sessionId === selectedSessionId,
      keyboardFocused:
        nodeId === keyboardFocusedNodeId && cn.sessionId !== selectedSessionId,
      onSpawnsResolved,
      onOpenDetail,
      onMeasure,
      status,
      nodeId,
      windowStart: cn.windowStart,
      windowEnd: cn.windowEnd,
      isResumeInstance: cn.isResumeInstance,
    };
    return {
      id: nodeId,
      type: "compact",
      position: {
        x: (pos?.x ?? 0) - COMPACT_CARD_WIDTH / 2,
        y: (pos?.y ?? 0) - h / 2,
      },
      data,
      draggable: false,
    };
  });

  return { nodes, edges };
}
