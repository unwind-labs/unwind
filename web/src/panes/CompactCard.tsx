import { useEffect, useMemo, useRef } from "react";
import { Handle, Position, useUpdateNodeInternals } from "reactflow";
import {
  GitFork,
  Sparkles,
  Activity,
  CheckCircle2,
  ChevronRight,
  CornerDownLeft,
  Leaf,
  Telescope,
  Trees,
  Loader2,
} from "lucide-react";
import { useMessages } from "@/api/client";
import { cn, shortId } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import type { TokenCost, TokenUsage } from "@/api/types";
import { filterExtrasByWindow, filterMessagesByWindow } from "./instances";
import { deriveRows, type Row } from "./derive-rows";
import { UsageFooter } from "./UsageFooter";

export { deriveRows } from "./derive-rows";

export const COMPACT_CARD_WIDTH = 380;
const RAIL_WIDTH = 42;
const ACTIVITY_HEIGHT = 28;
const SPAWN_HEIGHT = 36;
const HEADER_HEIGHT = 40;
const PADDING_Y = 8;
// Transposed footer table: 1 header row + 4 category rows. Leaf has
// 1 data column (Self); branch has 2 (Self + Subtree); root adds the
// $ column and the grand-total line below the table.
const FOOTER_HEIGHT_LEAF = 80;
const FOOTER_HEIGHT_BRANCH = 80;

export function estimateCardHeight(
  rows: Row[],
  opts?: { hasChildren?: boolean; hasUsage?: boolean },
): number {
  let h = HEADER_HEIGHT + PADDING_Y * 2;
  for (const r of rows) {
    h += r.kind === "spawn" ? SPAWN_HEIGHT : ACTIVITY_HEIGHT;
    h += 4; // gap
  }
  if (opts?.hasUsage) {
    h += opts.hasChildren ? FOOTER_HEIGHT_BRANCH : FOOTER_HEIGHT_LEAF;
  }
  return Math.max(h, HEADER_HEIGHT + PADDING_Y * 2);
}

export type CompactCardData = {
  slug: string;
  /** Underlying Claude session id — what ``useMessages`` is keyed by, and
   *  what ``onOpenDetail`` opens. Multiple cards on the canvas can share
   *  the same ``sessionId`` (one per ``invoke`` / ``invoke_resume``); they
   *  differ by their unique ReactFlow node id (= ``window_id``). */
  sessionId: string;
  /** Display label — task name from parent if known, else session id prefix. */
  label: string;
  isRoot: boolean;
  selected: boolean;
  /** Called when the user wants the full session view. */
  onOpenDetail: (sessionId: string) => void;
  /** Called when the rendered card height changes (for re-layout). Keyed by
   *  node id so two cards for the same session don't trample. */
  onMeasure: (cardNodeId: string, height: number) => void;
  status: "live" | "yield" | "done";
  /** Max status across this window AND every descendant (``live`` >
   *  ``yield`` > ``done``). Drives the rail indicator + yield wash so
   *  an ancestor visibly reflects work still happening below. The
   *  in-card terminator row (COMPLETE / YIELD) keeps using ``status``
   *  because it's a strictly self-only signal. */
  subtreeStatus: "live" | "yield" | "done";
  /** True when the keyboard cursor is on this card (arrow-key navigation
   *  with the right pane focused). Distinct from `selected`, which means
   *  the detail overlay is currently open for this session. */
  keyboardFocused?: boolean;
  /** Unique ReactFlow node id (= the backend's ``window_id`` — usually
   *  ``<session_id>#<window_index>``, except the root which is just the
   *  session id). Forwarded through ``onMeasure`` so the canvas keys
   *  measurements per-instance. */
  nodeId: string;
  /** Direct children of this card in the canvas tree, in the order
   *  they should be matched against the in-card spawn rows. Each spawn
   *  row picks up the next-unused child whose ``session_id`` matches
   *  the row's ``childId`` and renders its source Handle with that
   *  child's ``window_id`` — so the canvas's edges can anchor to a
   *  specific row instead of stacking on a default handle. */
  canvasChildren: { window_id: string; session_id: string }[];
  /** Inclusive ISO start of this instance's activity window. ``null`` for
   *  the root and for the first instance with no parent timestamp. */
  windowStart: string | null;
  /** Exclusive ISO end of this instance's activity window. ``null`` for the
   *  latest instance (open-ended). */
  windowEnd: string | null;
  /** True for invoke_resume windows or any non-first window of a session
   *  under one parent. Drives the "↻ resumed" pill. */
  isResumeInstance: boolean;
  /** Spawn kind from the parent's perspective: ``"call"`` if launched
   *  through ``/call``, ``"subagent"`` if launched as a Claude Code
   *  subagent. ``null`` for the root. Drives the rail's tint and label. */
  spawnKind: "call" | "subagent" | null;
  /** Called when a CALL/SUBAGENT row inside this card is clicked. The
   *  row's child window_id is passed so the canvas can pan/center it
   *  WITHOUT opening the detail overlay. ``undefined`` when the row's
   *  child isn't on the canvas yet — the row click then falls through
   *  to the default card click (open detail). */
  onFocusChild?: (windowId: string) => void;
  /** Tokens spent inside this window alone. Drives the footer row. */
  selfUsage: TokenUsage;
  /** Cumulative tokens for this window + descendants. Renders as the
   *  second footer row on intermediate nodes; collapsed for leaves
   *  (which have no children, so it equals ``selfUsage``). */
  subtreeUsage: TokenUsage;
  /** Cumulative USD cost for this window + descendants. Only rendered
   *  on the root card (as a third footer row with per-category $ values
   *  and a grand total). Always present so future cards can opt in
   *  without a new field. */
  subtreeCost: TokenCost;
  /** True iff this card has descendant cards on the canvas. Drives the
   *  2-row vs 1-row footer layout. Computed at canvas-tree build time
   *  rather than from ``canvasChildren`` so it stays stable even while
   *  child sessions are still resolving. */
  hasCanvasDescendants: boolean;
};

export function CompactCardNode({ data }: { data: CompactCardData }) {
  const { data: messages } = useMessages(data.slug, data.sessionId, false);
  // Filter the child's full message stream to this window so each card on
  // the canvas only shows what happened during its own slice of the session.
  // Earlier slices stop at the next invoke_resume; the latest is open-ended.
  const windowed = useMemo(() => {
    if (!messages) return null;
    const filtered = filterMessagesByWindow(
      messages.messages,
      data.windowStart,
      data.windowEnd,
    );
    const extras = filterExtrasByWindow(
      messages.extra_spawns,
      data.windowStart,
      data.windowEnd,
    );
    return { messages: filtered, extras };
  }, [messages, data.windowStart, data.windowEnd]);
  // Pass the FULL unwindowed message stream as the third arg so spawn
  // rows fired in this window still flip to "done" when their
  // tool_result lands in a later window (after a yield/resume).
  // Memo-ed: every parent re-render would otherwise re-walk the entire
  // window for every card, even when no inputs changed.
  const rows: Row[] = useMemo(
    () =>
      windowed
        ? deriveRows(
            windowed.messages,
            windowed.extras,
            messages?.messages ?? windowed.messages,
          )
        : [],
    [windowed, messages],
  );
  const cardRef = useRef<HTMLDivElement | null>(null);

  // Spawn handles are added/removed dynamically as `useMessages` resolves and
  // spawn rows appear. ReactFlow's nodeInternals map is built from the
  // handles present on the node's *first* render, and without an explicit
  // notification it doesn't re-scan when handles change. The visible symptom:
  // the node sits with `visibility: hidden` forever (its
  // dimensions/handles are considered un-measured). Telling RF that this
  // node's internals changed forces a re-scan and lifts the hidden flag.
  const updateNodeInternals = useUpdateNodeInternals();
  const handleSignature = useMemo(() => {
    const ids: string[] = [];
    for (const r of rows) if (r.kind === "spawn") ids.push(r.handleId);
    return ids.join("|");
  }, [rows]);
  useEffect(() => {
    updateNodeInternals(data.nodeId);
  }, [handleSignature, data.nodeId, updateNodeInternals]);
  // Status priority:
  //   1. Bounded window (``windowEnd != null``) — always ``done``: this slice
  //      ended when the next invoke_resume started.
  //   2. Authoritative session status from canvas (process detection + JSONL
  //      mtime fallback). If "yield" or "live", trust it.
  //   3. Otherwise, infer from spawn rows: any unfinished call → live.
  //   4. Otherwise → done.
  // Trust the backend's status verbatim — the canvas tree builder
  // already has the authoritative view (yield iff this window's task
  // status was ``yielded``; done otherwise). Earlier logic forced
  // ``done`` for any non-latest window, which masked yields on
  // historically-paused-then-resumed slices.
  const selfStatus: "live" | "yield" | "done" = data.status;
  // Visual rail/wash reflects the subtree: a root whose children are
  // still working should pulse live even though its own turn ended.
  // ``selfStatus`` remains the source of truth for the in-card
  // terminator row.
  const railStatus: "live" | "yield" | "done" = data.subtreeStatus;

  // Report measured height up so dagre can re-layout.
  useEffect(() => {
    const el = cardRef.current;
    if (!el) return;
    const obs = new ResizeObserver((entries) => {
      for (const e of entries) {
        data.onMeasure(data.nodeId, e.contentRect.height);
      }
    });
    obs.observe(el);
    return () => obs.disconnect();
  }, [data]);

  // Border color priority (highest wins):
  //   1. selected (detail open)  — Tailwind class chain below.
  //   2. keyboardFocused         — primary tint (light foreground in dark)
  //   3. default border-border.
  // Hover uses the saturated sky-blue accent (set via Tailwind classes
  // below). Keyboard cursor uses the calmer primary tint so the two
  // signals stay distinguishable.
  // The outline gets the same color so the visual edge thickens to 2px
  // without any layout shift (see hover comment below).
  // No background changes from hover/focus — only border + shadow.
  const cursorColor = "hsl(var(--primary) / 0.7)";
  const borderColor =
    !data.selected && data.keyboardFocused ? cursorColor : undefined;
  const outlineColor =
    !data.selected && data.keyboardFocused ? cursorColor : undefined;

  // Pop-style mapping from this card's spawn rows to the canvas tree's
  // child windows: each row claims the next-unused child whose
  // ``session_id`` matches the row's ``childId``. Built fresh on every
  // render — ``takeMatchingChild`` mutates the queues via ``.shift()``,
  // so a memoised Map stays drained after the first render and every
  // subsequent render sees no matches (breaking edge anchoring AND the
  // row click/hover focus path).
  const rowChildAssign = new Map<string, string[]>();
  for (const c of data.canvasChildren ?? []) {
    const list = rowChildAssign.get(c.session_id) ?? [];
    list.push(c.window_id);
    rowChildAssign.set(c.session_id, list);
  }

  // Kind drives the rail tint and the rotated label. Resume wins over
  // spawnKind so users immediately see "this is a continuation" in the
  // rail label rather than buried in a sub-line.
  const kind: "root" | "call" | "subagent" | "resume" = data.isRoot
    ? "root"
    : data.isResumeInstance
      ? "resume"
      : (data.spawnKind ?? "call");
  const railTintClass = {
    root: "from-slate-500/10 to-slate-500/0",
    call: "from-sky-500/10 to-sky-500/0",
    subagent: "from-violet-500/10 to-violet-500/0",
    resume: "from-amber-500/10 to-amber-500/0",
  }[kind];
  const shortSessionId = data.sessionId.startsWith("agent-")
    ? data.sessionId.slice(6, 14)
    : shortId(data.sessionId);

  return (
    <div
      ref={cardRef}
      className={cn(
        // nopan/nodrag: see comment in CanvasPane — required so d3-zoom
        // doesn't preventDefault on mousedown and break ReactFlow's
        // onNodeClick (which is what restores pointer-events on the
        // wrapper).
        // 1px border always, plus a 1px outline on hover/selected to
        // visually thicken to 2px. Outline doesn't affect layout, so the
        // card's bounding box is identical across states and React Flow
        // doesn't reflow neighbours.
        "nopan nodrag flex overflow-hidden rounded-2xl border border-border bg-card text-card-foreground shadow-sm transition-[border-color,box-shadow,outline-color]",
        "outline outline-1 outline-transparent",
        // Hover/focus: outline picks up the primary tint + drop shadow.
        // Focus styling is driven by a CSS rule in index.css that targets
        // the React Flow node wrapper (which is what actually receives
        // keyboard focus), not this inner div.
        "uw-compact-card hover:border-sky-400/70 hover:outline-sky-400/70 hover:shadow-lg",
        // Currently-open state: solid primary border + matching outline + subtle fill.
        data.selected && "border-primary outline-primary bg-primary/10 hover:border-primary hover:outline-primary",
        // Yielded: bold amber background so the whole card pops in the
        // canvas — the session is paused waiting for user input. The
        // amber wash is applied via the .uw-card-yield CSS rule (see
        // index.css) because the dark gradient surface uses the
        // ``background`` shorthand and would stomp Tailwind bg-* classes.
        railStatus === "yield" && "uw-card-yield",
      )}
      style={{
        width: COMPACT_CARD_WIDTH,
        cursor: "pointer",
        ...(borderColor ? { borderColor } : null),
        ...(outlineColor ? { outlineColor } : null),
      }}
    >
      {/* Default source handle on the right edge, vertically centered
          with the header. ReactFlow anchors any edge that doesn't
          specify a sourceHandle to the FIRST source-type handle on the
          node — so this MUST come before the per-spawn-row source
          handles below, otherwise all default-source edges would all
          stack on the first row. */}
      <Handle
        type="source"
        position={Position.Right}
        id="out"
        isConnectable={false}
        className="!h-2 !w-2 !border-0 !bg-transparent"
        style={{ top: HEADER_HEIGHT / 2 }}
      />
      {/* Incoming edge target on the left side, vertically centered with the header. */}
      <Handle
        type="target"
        position={Position.Left}
        id="in"
        isConnectable={false}
        className="!h-2 !w-2 !border-0 !bg-muted-foreground/40"
        style={{ top: HEADER_HEIGHT / 2 }}
      />
      {/* Left accent rail: status dot at top, rotated kind label in the
          middle, short session id at the bottom. The rail is tinted by
          a kind-specific gradient that matches the edge palette
          (sky=call, violet=subagent, amber=resume, neutral=root). */}
      <div
        className={cn(
          "flex shrink-0 flex-col items-center gap-2 border-r border-border/60 bg-gradient-to-b pb-4 pt-3",
          railTintClass,
        )}
        style={{ width: RAIL_WIDTH }}
      >
        <RailStatus status={railStatus} />
        <span
          className="text-[8.5px] font-bold uppercase tracking-[0.32em] text-foreground/70"
          style={{ writingMode: "vertical-rl", transform: "rotate(180deg)" }}
        >
          {railStatus === "yield"
            ? "waiting"
            : kind === "resume"
              ? "continued"
              : kind}
        </span>
      </div>
      <div className="min-w-0 flex-1">
        <header className="flex items-center justify-between gap-2 border-b border-border/60 px-4 py-3">
          <div className="flex min-w-0 flex-1 items-center gap-1.5">
            {data.selected || data.keyboardFocused ? (
              <Telescope
                className="h-3.5 w-3.5 shrink-0 text-primary"
                aria-label="current node"
                strokeWidth={2.25}
              />
            ) : null}
            {data.isResumeInstance ? (
              <ChevronRight
                className="h-3.5 w-3.5 shrink-0 text-amber-400"
                aria-label="continued"
                strokeWidth={2.5}
              />
            ) : null}
            <div className="truncate text-[12px] font-medium text-foreground">
              {data.label}
            </div>
          </div>
          <span className="font-mono text-[10px] text-muted-foreground/70">
            {shortSessionId}
          </span>
        </header>
        <ul className="flex flex-col gap-1 p-3">
          {!messages && (
            <li className="px-2 py-1 text-[10px] italic text-muted-foreground">
              loading…
            </li>
          )}
          {rows.map((r, i) => {
            if (r.kind === "activity") {
              return (
                <ActivityRow
                  key={i}
                  count={r.count}
                  spanSeconds={r.spanSeconds}
                />
              );
            }
            // Anchor the row's source Handle to its corresponding canvas
            // child window — pop the next-unused canvas child whose
            // ``session_id`` matches this row's ``childId``. Without this
            // override, every default-source edge would stack on the
            // first row. The popped window_id is also reused as the
            // focus target when the user clicks the row.
            const targetWindowId = takeMatchingChild(rowChildAssign, r.childId);
            return (
              <SpawnRowDisplay
                key={i}
                row={r}
                handleIdOverride={targetWindowId}
                onFocus={
                  targetWindowId && data.onFocusChild
                    ? () => data.onFocusChild!(targetWindowId)
                    : undefined
                }
              />
            );
          })}
          {messages && rows.length === 0 && (
            <li className="px-2 py-1 text-[10px] italic text-muted-foreground">
              (empty)
            </li>
          )}
          {/* Terminator row: every window that ended its work closes
              with a single line indicating HOW it ended — either it
              returned to its parent (COMPLETE) or paused waiting for
              user input (YIELD). No source handle, no status badge —
              the icon + label IS the indicator.

              Exception: the root card never shows COMPLETE. A "done"
              main session usually means "stale, resumable via
              ``claude --resume``", not "finished returning to a
              caller" — the COMPLETE label would be misleading. The
              root still shows the YIELD terminator when it's
              actively waiting for user input. */}
          {selfStatus === "done" && !data.isRoot && (
            <TerminatorRow kind="complete" />
          )}
          {selfStatus === "yield" && <TerminatorRow kind="yield" />}
        </ul>
        <UsageFooter
          self={data.selfUsage}
          subtree={data.subtreeUsage}
          subtreeCost={data.subtreeCost}
          showSubtree={data.hasCanvasDescendants}
          showCost={true}
        />
      </div>
    </div>
  );
}

function TerminatorRow({ kind }: { kind: "complete" | "yield" }) {
  const isYield = kind === "yield";
  const accentText = isYield ? "text-amber-300" : "text-emerald-400";
  const label = isYield ? "waiting for user" : "complete";
  return (
    <li
      className="flex items-center gap-2 rounded px-2 text-[11px] font-mono"
      style={{ height: ACTIVITY_HEIGHT - 4 }}
    >
      {isYield ? (
        <Loader2 className={cn("h-3.5 w-3.5 animate-spin", accentText)} />
      ) : (
        <CornerDownLeft className={cn("h-3.5 w-3.5", accentText)} />
      )}
      <span
        className={cn(
          "text-[9px] font-bold uppercase tracking-[0.18em]",
          accentText,
        )}
      >
        {label}
      </span>
    </li>
  );
}

/** Small status indicator at the top of the rail. Replaces the
 *  pre-rail top-border-color trick (border-t-emerald, border-t-amber). */
function RailStatus({ status }: { status: "live" | "yield" | "done" }) {
  if (status === "live") {
    return (
      <span className="relative inline-flex h-3 w-3">
        <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-70" />
        <span className="relative inline-flex h-3 w-3 rounded-full bg-emerald-400" />
      </span>
    );
  }
  if (status === "yield") {
    return <span className="inline-block h-3 w-3 rounded-full bg-amber-400" />;
  }
  return <CheckCircle2 className="h-5 w-5 text-emerald-400" />;
}

function ActivityRow({ count, spanSeconds }: { count: number; spanSeconds: number }) {
  return (
    <li
      className="flex items-center gap-2 rounded border border-transparent px-2 text-[11px] text-muted-foreground"
      style={{ height: ACTIVITY_HEIGHT - 4 }}
    >
      <Activity className="h-3 w-3 opacity-60" />
      <span>activity</span>
      <span className="opacity-60">·</span>
      <span>{count} msgs</span>
      {spanSeconds > 0 ? (
        <>
          <span className="opacity-60">·</span>
          <span className="tabular-nums">{formatSpan(spanSeconds)}</span>
        </>
      ) : null}
    </li>
  );
}

/** Pop the next-unused canvas child window whose ``session_id`` matches
 *  ``childId``. Returns ``undefined`` (so the Handle keeps its derive-rows
 *  fallback id) when the canvas tree doesn't know about this spawn yet,
 *  or when the row's child is empty. */
function takeMatchingChild(
  pool: Map<string, string[]>,
  childId: string | null | undefined,
): string | undefined {
  if (!childId) return undefined;
  const queue = pool.get(childId);
  if (!queue || queue.length === 0) return undefined;
  return queue.shift();
}

function SpawnRowDisplay({
  row,
  handleIdOverride,
  onFocus,
}: {
  row: Extract<Row, { kind: "spawn" }>;
  handleIdOverride?: string;
  /** Click handler — when set, the row owns its own click and stops it
   *  from bubbling up to the card-level "open detail" handler. */
  onFocus?: () => void;
}) {
  const isCall = row.spawnKind === "call";
  // Sub-variant of a call: fork (default, inherits ctx), fresh (isolated,
  // same project), fresh_cross_project (isolated, different project).
  const isFresh = isCall && row.callType === "fresh";
  const isFreshCross = isCall && row.callType === "fresh_cross_project";
  // Resume rows ("CONTINUED") get the amber treatment — same yellow
  // we use for yielded windows, since each resume line was waiting on
  // the user before it ran.
  const accentText = row.isResume
    ? "text-amber-300"
    : isFreshCross
      ? "text-teal-300"
      : isFresh
        ? "text-emerald-300"
        : isCall
          ? "text-sky-300"
          : "text-violet-300";
  const accentBg = row.isResume
    ? "bg-amber-500/5"
    : isFreshCross
      ? "bg-teal-950/40"
      : isFresh
        ? "bg-emerald-950/40"
        : isCall
          ? "bg-sky-950/40"
          : "bg-violet-950/40";
  const accentBorder = row.isResume
    ? "border-amber-500/50"
    : isFreshCross
      ? "border-teal-500/50"
      : isFresh
        ? "border-emerald-500/50"
        : isCall
          ? "border-sky-500/50"
          : "border-violet-500/50";
  const badgeBorderText = row.isResume
    ? "border-amber-500/40 text-amber-300"
    : isFreshCross
      ? "border-teal-500/40 text-teal-300"
      : isFresh
        ? "border-emerald-500/40 text-emerald-300"
        : isCall
          ? "border-sky-500/40 text-sky-300"
          : "border-violet-500/40 text-violet-300";

  return (
    <li
      className={cn(
        "uw-spawn-row relative flex items-center gap-2 rounded border-l-2 px-2 transition-colors",
        accentBg,
        accentBorder,
        // Row owns its own hover state — the card's ``:has(.uw-spawn-row:hover)``
        // CSS rule (in index.css) suppresses the card-level hover ring
        // while the cursor is over a row. We use an INSET ring + brighter
        // background so the affordance is visible despite the card's
        // ``overflow-hidden`` (an outward ring would be clipped).
        onFocus && "cursor-pointer hover:ring-2 hover:ring-inset",
        onFocus &&
          (row.isResume
            ? "hover:bg-amber-500/15 hover:ring-amber-400/70"
            : isFreshCross
              ? "hover:bg-teal-900/60 hover:ring-teal-400/70"
              : isFresh
                ? "hover:bg-emerald-900/60 hover:ring-emerald-400/70"
                : isCall
                  ? "hover:bg-sky-900/60 hover:ring-sky-400/70"
                  : "hover:bg-violet-900/60 hover:ring-violet-400/70"),
      )}
      style={{ height: SPAWN_HEIGHT - 4 }}
      onClick={
        onFocus
          ? (e) => {
              e.stopPropagation();
              onFocus();
            }
          : undefined
      }>
      {!isCall ? (
        <Sparkles className={cn("h-3 w-3", accentText)} />
      ) : isFreshCross ? (
        <Trees className={cn("h-3 w-3", accentText)} />
      ) : isFresh ? (
        <Leaf className={cn("h-3 w-3", accentText)} />
      ) : (
        <GitFork className={cn("h-3 w-3", accentText)} />
      )}
      <Badge
        variant="outline"
        className={cn("border text-[9px] uppercase", badgeBorderText)}
      >
        {row.isResume
          ? "continued"
          : !isCall
            ? "subagent"
            : isFreshCross
              ? "fresh @ other"
              : isFresh
                ? "fresh"
                : "call"}
      </Badge>
      <span
        className="flex-1 truncate font-mono text-[11px] text-foreground"
        title={row.title}
      >
        {row.title}
      </span>
      {row.done ? (
        <CheckCircle2 className="h-4 w-4 text-emerald-400" />
      ) : (
        <DotPulse />
      )}
      <Handle
        type="source"
        position={Position.Right}
        id={handleIdOverride ?? row.handleId}
        isConnectable={false}
        className="!h-2 !w-2 !border-0 !bg-muted-foreground/60"
      />
    </li>
  );
}

function DotPulse() {
  return (
    <span className="inline-flex items-center gap-1">
      <span className="dot-pulse-1 h-1.5 w-1.5 rounded-full bg-amber-400" />
      <span className="dot-pulse-2 h-1.5 w-1.5 rounded-full bg-amber-400" />
      <span className="dot-pulse-3 h-1.5 w-1.5 rounded-full bg-amber-400" />
    </span>
  );
}

function formatSpan(s: number): string {
  if (s < 1) return `${Math.round(s * 1000)}ms`;
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return `${m}m ${sec}s`;
}

