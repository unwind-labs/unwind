import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  ListTree,
  Telescope,
  XCircle,
} from "lucide-react";
import { useCanvasTree } from "@/api/client";
import type { WindowNode } from "@/api/types";
import { useUi } from "@/store/ui";
import { navigate } from "@/lib/url-sync";
import { isTypingTarget } from "@/lib/keyboard";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Badge } from "@/components/ui/badge";
import { cn, shortId } from "@/lib/utils";
import { UsageFooter } from "./UsageFooter";
import {
  type FlatRow,
  flattenTree,
  leftAction,
  nextRowId,
  rightAction,
  treeSize,
} from "./tree-nav";

type DetailWin = { start: string | null; end: string | null } | null;

/** Lightweight text view of a session's call tree — the alternative to the
 *  graphical canvas, defaulted to for complex runs (see ``isComplexTree``).
 *  Renders the SAME ``useCanvasTree`` data as a single-vertical-scroll nested
 *  list, with a hover/focus overlay that previews the focused node "as a card
 *  without the activity rows". No per-node ``useMessages`` fetches — the whole
 *  view (and overlay) renders from the window tree alone, which is what keeps
 *  it cheap where the canvas is heavy. */
export function FolderTreePane({
  slug,
  rootSessionId,
  onOpenDetail,
}: {
  slug: string;
  rootSessionId: string;
  onOpenDetail: (id: string, window?: DetailWin) => void;
}) {
  const { data: canvasTree } = useCanvasTree(slug, rootSessionId);

  // The focused node is the shared canvas cursor (a window_id) — so flipping
  // Canvas↔Text keeps the same node highlighted, and it's already URL-synced.
  const focusedId = useUi((s) => s.canvasFocusedNodeId);
  const focusedPane = useUi((s) => s.focusedPane);
  const detailOpen = useUi((s) => !!s.detailSessionId);
  const canvasEnterIntent = useUi((s) => s.canvasEnterIntent);
  const clearCanvasEnterIntent = useUi((s) => s.clearCanvasEnterIntent);

  // Collapse state is local and ephemeral; ``hovered`` drives the overlay for
  // mouse users (keyboard users get the overlay off the persistent focus).
  // ``collapsed === null`` is the fresh per-root state: it means "use the
  // one-level default" (root's children shown, deeper subtrees folded) so a
  // big tree opens scannable instead of fully exploded. The first
  // expand/collapse replaces null with the user's explicit set.
  const [collapsed, setCollapsed] = useState<Set<string> | null>(null);
  const [hovered, setHovered] = useState<string | null>(null);
  useEffect(() => {
    setCollapsed(null);
    setHovered(null);
  }, [rootSessionId]);

  // Non-root parents — the set that "one level" / "collapse all" folds. Walks
  // the REACHABLE tree (not ``all_windows``, which carries orphan windows).
  const collapsibleIds = useMemo(() => {
    if (!canvasTree) return [];
    const ids: string[] = [];
    const walk = (n: WindowNode, isRoot: boolean) => {
      if (n.children.length > 0 && !isRoot) ids.push(n.window_id);
      for (const c of n.children) walk(c, false);
    };
    walk(canvasTree.root, true);
    return ids;
  }, [canvasTree]);

  // Resolve null → the one-level default. Memoised so ``rows`` doesn't recompute
  // every render while the default is in effect.
  const effectiveCollapsed = useMemo(
    () => collapsed ?? new Set(collapsibleIds),
    [collapsed, collapsibleIds],
  );

  const rows = useMemo(
    () => (canvasTree ? flattenTree(canvasTree.root, effectiveCollapsed) : []),
    [canvasTree, effectiveCollapsed],
  );
  const nodeById = useMemo(() => {
    const m = new Map<string, WindowNode>();
    for (const w of canvasTree?.all_windows ?? []) m.set(w.window_id, w);
    return m;
  }, [canvasTree]);

  const setFocus = useCallback((id: string | null) => navigate.setCanvasFocus(id), []);
  const toggleCollapse = useCallback(
    (id: string) => {
      // Seed from the one-level default when the user hasn't touched it yet, so
      // their first toggle edits the visible state rather than a blank set.
      setCollapsed((prev) => {
        const next = new Set(prev ?? collapsibleIds);
        if (next.has(id)) next.delete(id);
        else next.add(id);
        return next;
      });
    },
    [collapsibleIds],
  );
  const openNode = useCallback(
    (id: string) => {
      const node = nodeById.get(id);
      if (!node) return;
      onOpenDetail(node.session_id, { start: node.window_start, end: node.window_end });
    },
    [nodeById, onOpenDetail],
  );

  // When entering the pane via keyboard (←/→ pane rotation), land the cursor
  // on the first row so the first ↑/↓ has an anchor — same signal the canvas
  // consumes. Only one of the two views is mounted at a time, so there's no
  // contention over ``canvasEnterIntent``.
  useEffect(() => {
    if (canvasEnterIntent !== "keyboard") return;
    if (rows.length === 0) return;
    if (focusedId === null) setFocus(rows[0].node.window_id);
    clearCanvasEnterIntent();
  }, [canvasEnterIntent, focusedId, rows, setFocus, clearCanvasEnterIntent]);

  // Latest nav state, read by the window keydown listener (attached once) so
  // it never goes stale without re-binding on every focus/collapse change.
  const navRef = useRef({
    rows,
    focusedId,
    collapsed: effectiveCollapsed,
    focusedPane,
    detailOpen,
  });
  navRef.current = { rows, focusedId, collapsed: effectiveCollapsed, focusedPane, detailOpen };

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (isTypingTarget(e)) return;
      const s = navRef.current;
      // Only the thread pane owns these keys, and never while the detail
      // overlay (rendered above this pane by CanvasPane) is open.
      if (s.focusedPane !== "thread" || s.detailOpen) return;
      const isUp = e.key === "ArrowUp" || e.key === "k";
      const isDown = e.key === "ArrowDown" || e.key === "j";
      const isLeft = e.key === "ArrowLeft" || e.key === "h";
      const isRight = e.key === "ArrowRight" || e.key === "l";
      const isEnter = e.key === "Enter";
      const isEsc = e.key === "Escape";
      if (!isUp && !isDown && !isLeft && !isRight && !isEnter && !isEsc) return;
      e.preventDefault();

      if (isEsc) {
        if (s.focusedId) setFocus(null);
        return;
      }
      if (isEnter) {
        if (s.focusedId) openNode(s.focusedId);
        return;
      }
      if (isUp || isDown) {
        setFocus(nextRowId(s.rows, s.focusedId, isDown ? 1 : -1));
        return;
      }
      if (isRight) {
        const r = rightAction(s.rows, s.focusedId, s.collapsed);
        if (r.type === "expand") toggleCollapse(r.id);
        else if (r.type === "focus") setFocus(r.id);
        return;
      }
      // isLeft
      const r = leftAction(s.rows, s.focusedId, s.collapsed);
      if (r.type === "collapse") toggleCollapse(r.id);
      else if (r.type === "focus") setFocus(r.id);
      // Nowhere left to go (the root) → hand focus back to the session list,
      // mirroring the canvas's ← behaviour.
      else useUi.getState().rotateFocus(-1);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [setFocus, toggleCollapse, openNode]);

  // Expand-all / collapse-all. Collapse-all keeps the ROOT open (collapsing it
  // would hide the whole run behind one row) and folds every deeper parent —
  // the same one-level overview a freshly-opened tree starts in.
  const anyCollapsed = effectiveCollapsed.size > 0;
  const toggleAll = () => setCollapsed(anyCollapsed ? new Set() : new Set(collapsibleIds));

  const overlayNode = (() => {
    const id = hovered ?? focusedId;
    return id ? (nodeById.get(id) ?? null) : null;
  })();

  return (
    <div className="relative flex h-full flex-col">
      {/* pr-16 reserves room for CanvasPane's floating view toggle, which
          overlays the top-right of this pane. */}
      <header className="flex items-center gap-2 border-b border-border/60 py-2 pl-3 pr-16">
        <ListTree className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        <span className="min-w-0 flex-1 truncate text-[11px] font-medium text-foreground">
          {canvasTree?.root.label ?? "…"}
        </span>
        {canvasTree ? (
          <span className="shrink-0 font-mono text-[10px] text-muted-foreground/70">
            {treeSize(canvasTree.root)} nodes
          </span>
        ) : null}
        {collapsibleIds.length > 0 ? (
          <button
            type="button"
            onClick={toggleAll}
            className="shrink-0 rounded px-1.5 py-0.5 text-[10px] text-muted-foreground transition-colors hover:bg-accent/50 hover:text-foreground"
          >
            {anyCollapsed ? "expand all" : "collapse all"}
          </button>
        ) : null}
      </header>
      <ScrollArea className="flex-1">
        {!canvasTree ? (
          <div className="px-3 py-4 text-xs text-muted-foreground">loading…</div>
        ) : (
          <ul className="py-1">
            {rows.map((row) => (
              <TreeRow
                key={row.node.window_id}
                row={row}
                focused={row.node.window_id === focusedId}
                onToggle={() => toggleCollapse(row.node.window_id)}
                onOpen={() => {
                  setFocus(row.node.window_id);
                  openNode(row.node.window_id);
                }}
                onHover={() => setHovered(row.node.window_id)}
                onLeave={() => setHovered((h) => (h === row.node.window_id ? null : h))}
              />
            ))}
          </ul>
        )}
      </ScrollArea>
      {overlayNode ? (
        <NodeSummaryCard node={overlayNode} isFocused={overlayNode.window_id === focusedId} />
      ) : null}
    </div>
  );
}

// --- one row ----------------------------------------------------------------

const INDENT_PER_DEPTH = 14;

function TreeRow({
  row,
  focused,
  onToggle,
  onOpen,
  onHover,
  onLeave,
}: {
  row: FlatRow;
  focused: boolean;
  onToggle: () => void;
  onOpen: () => void;
  onHover: () => void;
  onLeave: () => void;
}) {
  const { node, depth, hasChildren, collapsed } = row;
  const ref = useRef<HTMLDivElement | null>(null);
  // Keep the soft cursor in view AND move real DOM focus onto it, so the
  // keyboard cursor and document.activeElement stay unified (mirrors
  // SessionListPane). ``block: "nearest"`` + ``preventScroll`` avoid yanking
  // the list around. The visible focus ring is suppressed in the className —
  // the cursor highlight (bg + left bar) is the focus affordance instead.
  useEffect(() => {
    if (!focused) return;
    ref.current?.scrollIntoView({ block: "nearest" });
    ref.current?.focus({ preventScroll: true });
  }, [focused]);

  const cost =
    node.subtree_cost.cw + node.subtree_cost.cr + node.subtree_cost.r + node.subtree_cost.w;

  return (
    <li>
      <div
        ref={ref}
        role="button"
        tabIndex={-1}
        onClick={onOpen}
        onMouseEnter={onHover}
        onMouseLeave={onLeave}
        className={cn(
          "relative flex w-full cursor-pointer items-center gap-2 py-1 pr-3 text-left transition-colors",
          // Suppress the browser focus rect — the cursor highlight below is
          // the affordance (matches the session list's flat-row treatment).
          "outline-none focus:outline-none focus-visible:outline-none",
          "before:absolute before:inset-y-0 before:left-0 before:w-[2px] before:bg-transparent",
          "hover:bg-foreground/[0.04]",
          focused && "bg-foreground/[0.07] before:bg-primary",
        )}
        style={{ paddingLeft: 8 + depth * INDENT_PER_DEPTH }}
      >
        {hasChildren ? (
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              onToggle();
            }}
            aria-label={collapsed ? "expand" : "collapse"}
            className="shrink-0 rounded text-muted-foreground outline-none transition-colors hover:text-foreground focus:outline-none focus-visible:outline-none"
          >
            {collapsed ? (
              <ChevronRight className="h-3.5 w-3.5" />
            ) : (
              <ChevronDown className="h-3.5 w-3.5" />
            )}
          </button>
        ) : (
          <span className="h-3.5 w-3.5 shrink-0" />
        )}
        <StatusDot status={node.subtree_status} />
        <Badge
          variant="outline"
          className={cn("shrink-0 border px-1.5 py-0 text-[9px] uppercase", KIND_CHIP[node.kind])}
        >
          {KIND_LABEL[node.kind]}
        </Badge>
        {focused ? (
          <Telescope className="h-3 w-3 shrink-0 text-primary" strokeWidth={2.25} />
        ) : null}
        <span className="min-w-0 flex-1 truncate text-[12px] text-foreground">{node.label}</span>
        <span className="shrink-0 font-mono text-[10px] text-muted-foreground/60">
          {shortId(node.session_id)}
        </span>
        {cost > 0 ? (
          <span className="shrink-0 font-mono text-[10px] tabular-nums text-emerald-300/80">
            {fmtCost(cost)}
          </span>
        ) : null}
      </div>
    </li>
  );
}

// --- hover/focus overlay: the node "as a card", minus the activity rows -----

function NodeSummaryCard({ node, isFocused }: { node: WindowNode; isFocused: boolean }) {
  const status = node.subtree_status;
  const railLabel =
    status === "yield" ? "waiting" : status === "failed" ? "error" : RAIL_LABEL[node.kind];
  return (
    <div className="pointer-events-none absolute bottom-3 right-3 z-[5] w-[360px] max-w-[calc(100%-1.5rem)]">
      <div
        className={cn(
          // Reuse ``uw-compact-card`` so the surface + yield/live washes match
          // the real canvas card exactly (see index.css). Pointer-events-none
          // keeps the card's hover rules dormant.
          "uw-compact-card flex overflow-hidden rounded-2xl border border-border bg-card text-card-foreground shadow-xl",
          status === "yield" && "uw-card-yield",
          status === "live" && "uw-card-live",
        )}
      >
        <div
          className={cn(
            "flex shrink-0 flex-col items-center gap-2 border-r border-border/60 bg-gradient-to-b pb-4 pt-3",
            RAIL_TINT[node.kind],
          )}
          style={{ width: 42 }}
        >
          <StatusGlyph status={status} />
          <span
            className="text-[8.5px] font-bold uppercase tracking-[0.32em] text-foreground/70"
            style={{ writingMode: "vertical-rl", transform: "rotate(180deg)" }}
          >
            {railLabel}
          </span>
        </div>
        <div className="min-w-0 flex-1">
          <header className="flex items-center justify-between gap-2 border-b border-border/60 px-4 py-3">
            <div className="flex min-w-0 flex-1 items-center gap-1.5">
              {isFocused ? (
                <Telescope className="h-3.5 w-3.5 shrink-0 text-primary" strokeWidth={2.25} />
              ) : null}
              {node.kind === "resume" ? (
                <ChevronRight className="h-3.5 w-3.5 shrink-0 text-amber-400" strokeWidth={2.5} />
              ) : null}
              <div className="truncate text-[12px] font-medium text-foreground">{node.label}</div>
            </div>
            <span className="shrink-0 font-mono text-[10px] text-muted-foreground/70">
              {shortId(node.session_id)}
            </span>
          </header>
          <UsageFooter
            self={node.self_usage}
            subtree={node.subtree_usage}
            subtreeCost={node.subtree_cost}
            showSubtree={node.children.length > 0}
            isRoot={node.kind === "root"}
          />
        </div>
      </div>
    </div>
  );
}

// --- shared bits ------------------------------------------------------------

// Per-row inline dot: four-state status vocabulary matching the canvas rail.
function StatusDot({ status }: { status: WindowNode["subtree_status"] }) {
  if (status === "live") {
    return (
      <span className="relative inline-flex h-2 w-2 shrink-0" title="live">
        <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-70" />
        <span className="relative inline-flex h-2 w-2 rounded-full bg-emerald-400" />
      </span>
    );
  }
  if (status === "yield") {
    return (
      <span
        className="inline-block h-2 w-2 shrink-0 animate-pulse rounded-full bg-amber-400"
        title="waiting"
      />
    );
  }
  if (status === "failed") {
    return <span className="inline-block h-2 w-2 shrink-0 rounded-full bg-red-400" title="error" />;
  }
  return (
    <span
      className="inline-block h-2 w-2 shrink-0 rounded-full bg-muted-foreground/40"
      title="done"
    />
  );
}

// Larger glyph for the overlay rail, mirroring CompactCard's RailStatus.
function StatusGlyph({ status }: { status: WindowNode["subtree_status"] }) {
  if (status === "live") {
    return (
      <span className="relative inline-flex h-3 w-3">
        <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-70" />
        <span className="relative inline-flex h-3 w-3 rounded-full bg-emerald-400" />
      </span>
    );
  }
  if (status === "yield")
    return <span className="inline-block h-3 w-3 rounded-full bg-amber-400" />;
  if (status === "failed") return <XCircle className="h-5 w-5 text-red-400" />;
  return <CheckCircle2 className="h-5 w-5 text-emerald-400" />;
}

const KIND_LABEL: Record<WindowNode["kind"], string> = {
  root: "root",
  call: "call",
  subagent: "subagent",
  resume: "continued",
  workflow: "workflow",
  workflow_phase: "phase",
};

// Chip border+text colours mirror the canvas edge/rail palette: sky=call,
// violet=subagent, amber=resume, indigo=workflow, slate=root.
const KIND_CHIP: Record<WindowNode["kind"], string> = {
  root: "border-slate-500/40 text-slate-300",
  call: "border-sky-500/40 text-sky-300",
  subagent: "border-violet-500/40 text-violet-300",
  resume: "border-amber-500/40 text-amber-300",
  workflow: "border-indigo-500/40 text-indigo-300",
  workflow_phase: "border-indigo-500/40 text-indigo-300",
};

const RAIL_TINT: Record<WindowNode["kind"], string> = {
  root: "from-slate-500/10 to-slate-500/0",
  call: "from-sky-500/10 to-sky-500/0",
  subagent: "from-violet-500/10 to-violet-500/0",
  resume: "from-amber-500/10 to-amber-500/0",
  workflow: "from-indigo-500/10 to-indigo-500/0",
  workflow_phase: "from-indigo-500/10 to-indigo-500/0",
};

const RAIL_LABEL: Record<WindowNode["kind"], string> = {
  root: "root",
  call: "call",
  subagent: "subagent",
  resume: "continued",
  workflow: "workflow",
  workflow_phase: "phase",
};

/** Compact $ for a row: ``$0`` hidden by the caller, tiny non-zero shown as
 *  ``<$0.01`` so a real cost never reads as zero, else 2dp. */
function fmtCost(n: number): string {
  if (n < 0.01) return "<$0.01";
  return `$${n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}
