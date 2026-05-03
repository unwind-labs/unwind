import { useEffect, useMemo, useRef } from "react";
import { Handle, Position, useUpdateNodeInternals } from "reactflow";
import {
  GitFork,
  Sparkles,
  Activity,
  CheckCircle2,
  ChevronRight,
} from "lucide-react";
import { useMessages } from "@/api/client";
import { cn, shortId } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { filterMessagesByWindow } from "./instances";
import { deriveRows, type Row } from "./derive-rows";

export { deriveRows } from "./derive-rows";

export const COMPACT_CARD_WIDTH = 340;
const ACTIVITY_HEIGHT = 28;
const SPAWN_HEIGHT = 36;
const HEADER_HEIGHT = 40;
const PADDING_Y = 8;

export function estimateCardHeight(rows: Row[]): number {
  let h = HEADER_HEIGHT + PADDING_Y * 2;
  for (const r of rows) {
    h += r.kind === "spawn" ? SPAWN_HEIGHT : ACTIVITY_HEIGHT;
    h += 4; // gap
  }
  return Math.max(h, HEADER_HEIGHT + PADDING_Y * 2);
}

export type ResolvedSpawn = {
  handleId: string;
  childId: string;
  label: string;
  spawnKind: "call" | "subagent";
  done: boolean;
  parentToolUseTs: string | null;
  isResume: boolean;
  userReply?: string;
};

export type CompactCardData = {
  slug: string;
  /** Underlying Claude session id — what ``useMessages`` is keyed by, and
   *  what ``onOpenDetail`` opens. Multiple cards on the canvas can share
   *  the same ``sessionId`` (one per ``invoke`` / ``invoke_resume``); they
   *  differ by their unique ReactFlow node id. */
  sessionId: string;
  /** Display label — task name from parent if known, else session id prefix. */
  label: string;
  isRoot: boolean;
  selected: boolean;
  /** Called whenever this card discovers spawn rows so the canvas can add child nodes.
   *  ``cardNodeId`` is the parent card's ReactFlow node id (NOT its sessionId
   *  — instances of the same session have distinct node ids). */
  onSpawnsResolved: (cardNodeId: string, spawns: ResolvedSpawn[]) => void;
  /** Called when the user wants the full session view. */
  onOpenDetail: (sessionId: string) => void;
  /** Called when the rendered card height changes (for re-layout). Keyed by
   *  node id so two cards for the same session don't trample. */
  onMeasure: (cardNodeId: string, height: number) => void;
  status: "live" | "yield" | "done";
  /** True when the keyboard cursor is on this card (arrow-key navigation
   *  with the right pane focused). Distinct from `selected`, which means
   *  the detail overlay is currently open for this session. */
  keyboardFocused?: boolean;
  /** Unique ReactFlow node id (handleId for child instances; sessionId for
   *  the root). Forwarded back through ``onSpawnsResolved`` and ``onMeasure``
   *  so the canvas keys its state per-instance. */
  nodeId: string;
  /** Inclusive ISO start of this instance's activity window. ``null`` for
   *  the root and for the first instance with no parent timestamp. */
  windowStart: string | null;
  /** Exclusive ISO end of this instance's activity window. ``null`` for the
   *  latest instance (open-ended). */
  windowEnd: string | null;
  /** True for invoke_resume windows or any non-first window of a session
   *  under one parent. Drives the "↻ resumed" pill. */
  isResumeInstance: boolean;
};

export function CompactCardNode({ data }: { data: CompactCardData }) {
  const { data: messages } = useMessages(data.slug, data.sessionId, false);
  // ``windowEnd === null`` means this is the open-ended (latest) view of the
  // session — same shape as the root node, which has both ends null.
  const isLatest = data.windowEnd === null;
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
    // ``extra_spawns`` (callstack-Skill spawns without a parent tool_use)
    // have no per-window anchor — pin them to the latest window so we
    // don't fan them out across every resume.
    const extras = isLatest ? messages.extra_spawns ?? [] : [];
    return { messages: filtered, extras };
  }, [messages, data.windowStart, data.windowEnd, isLatest]);
  const rows: Row[] = windowed
    ? deriveRows(windowed.messages, windowed.extras)
    : [];
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
  const selfStatus: "live" | "yield" | "done" = !isLatest
    ? "done"
    : data.status === "yield"
      ? "yield"
      : data.status === "live"
        ? "live"
        : windowed && rows.some((r) => r.kind === "spawn" && !r.done)
          ? "live"
          : "done";

  // When spawn rows are discovered, tell the canvas so it can add child cards.
  useEffect(() => {
    if (!windowed) return;
    const spawns: ResolvedSpawn[] = [];
    for (const r of rows) {
      if (r.kind !== "spawn") continue;
      // Skip rows whose child hasn't resolved yet — no card to draw.
      if (!r.childId) continue;
      // Defensive: a spawn whose handleId matches our own nodeId would
      // create a self-loop in spawnsByParent and infinite-recurse the
      // layout walk. This shouldn't happen (handleIds are derived from
      // tool_use_ids unique to each session), but inherited-tool-use
      // edge cases can produce it; drop them rather than crashing.
      if (r.handleId === data.nodeId) continue;
      spawns.push({
        handleId: r.handleId,
        childId: r.childId,
        label: r.title,
        spawnKind: r.spawnKind,
        done: r.done,
        parentToolUseTs: r.parentToolUseTs,
        isResume: r.isResume,
        userReply: r.userReply,
      });
    }
    data.onSpawnsResolved(data.nodeId, spawns);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [windowed]);

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
  //   2. keyboardFocused         — color(srgb 0 0.54 0.8 / 0.8) (sky blue)
  //   3. default border-border.
  // The outline gets the same color so the visual edge thickens to 2px
  // without any layout shift (see hover comment below).
  // No background changes from hover/focus — only border + shadow.
  const focusBlue = "color(srgb 0 0.54 0.8 / 0.8)";
  const borderColor =
    !data.selected && data.keyboardFocused ? focusBlue : undefined;
  const outlineColor =
    !data.selected && data.keyboardFocused ? focusBlue : undefined;

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
        "nopan nodrag overflow-hidden rounded-2xl border border-border bg-card text-card-foreground shadow-sm transition-[border-color,box-shadow,outline-color]",
        "outline outline-1 outline-transparent",
        // Hover/focus: outline picks up the primary tint + drop shadow.
        // Focus styling is driven by a CSS rule in index.css that targets
        // the React Flow node wrapper (which is what actually receives
        // keyboard focus), not this inner div.
        "uw-compact-card hover:border-primary/60 hover:outline-primary/60 hover:shadow-lg",
        // Currently-open state: solid primary border + matching outline + subtle fill.
        data.selected && "border-primary outline-primary bg-primary/10 hover:border-primary hover:outline-primary",
        selfStatus === "live" && "border-t-emerald-500",
        // Yielded: bold amber background so it pops in the canvas — the
        // session is paused waiting for user input.
        selfStatus === "yield" &&
          "border-t-amber-400 bg-amber-500/25 hover:bg-amber-500/30",
        selfStatus === "yield" &&
          data.selected &&
          "bg-amber-500/35 hover:bg-amber-500/35",
      )}
      style={{
        width: COMPACT_CARD_WIDTH,
        cursor: "pointer",
        ...(borderColor ? { borderColor } : null),
        ...(outlineColor ? { outlineColor } : null),
      }}
    >
      {/* Incoming edge target on the left side, vertically centered with the header. */}
      <Handle
        type="target"
        position={Position.Left}
        id="in"
        isConnectable={false}
        className="!h-2 !w-2 !border-0 !bg-muted-foreground/40"
        style={{ top: HEADER_HEIGHT / 2 }}
      />
      <header className="flex items-center justify-between gap-2 border-b border-border px-4 py-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5">
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
          <div className="font-mono text-[10px] text-muted-foreground">
            {data.isRoot ? "root · " : ""}
            {data.sessionId.startsWith("agent-")
              ? data.sessionId.slice(6, 14)
              : shortId(data.sessionId)}
            {data.isResumeInstance ? (
              <span className="ml-1 text-amber-400/80">· continued</span>
            ) : null}
          </div>
        </div>
        {selfStatus === "yield" ? (
          <Badge variant="warn">yield</Badge>
        ) : selfStatus === "live" ? (
          <Badge variant="warn">live</Badge>
        ) : (
          <CheckCircle2 className="h-3 w-3 text-emerald-500/70" />
        )}
      </header>
      <ul className="flex flex-col gap-1 p-3">
        {!messages && (
          <li className="px-2 py-1 text-[10px] italic text-muted-foreground">
            loading…
          </li>
        )}
        {rows.map((r, i) =>
          r.kind === "activity" ? (
            <ActivityRow key={i} count={r.count} spanSeconds={r.spanSeconds} />
          ) : (
            <SpawnRowDisplay key={i} row={r} />
          ),
        )}
        {messages && rows.length === 0 && (
          <li className="px-2 py-1 text-[10px] italic text-muted-foreground">
            (empty)
          </li>
        )}
      </ul>
    </div>
  );
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

function SpawnRowDisplay({
  row,
}: {
  row: Extract<Row, { kind: "spawn" }>;
}) {
  const isCall = row.spawnKind === "call";
  const accentText = isCall ? "text-sky-300" : "text-violet-300";
  const accentBg = isCall ? "bg-sky-950/40" : "bg-violet-950/40";
  const accentBorder = isCall ? "border-sky-500/50" : "border-violet-500/50";

  return (
    <li
      className={cn(
        "relative flex items-center gap-2 rounded border-l-2 px-2",
        accentBg,
        accentBorder,
      )}
      style={{ height: SPAWN_HEIGHT - 4 }}
    >
      {isCall ? (
        <GitFork className={cn("h-3 w-3", accentText)} />
      ) : (
        <Sparkles className={cn("h-3 w-3", accentText)} />
      )}
      <Badge
        variant="outline"
        className={cn(
          "border text-[9px] uppercase",
          isCall
            ? "border-sky-500/40 text-sky-300"
            : "border-violet-500/40 text-violet-300",
        )}
      >
        {isCall ? "call" : "subagent"}
      </Badge>
      <span
        className="flex-1 truncate font-mono text-[11px] text-foreground"
        title={row.title}
      >
        {row.title}
      </span>
      {row.done ? (
        <CheckCircle2 className="h-3 w-3 text-emerald-500/70" />
      ) : (
        <DotPulse />
      )}
      <Handle
        type="source"
        position={Position.Right}
        id={row.handleId}
        isConnectable={false}
        className="!h-2 !w-2 !border-0 !bg-muted-foreground/60"
      />
    </li>
  );
}

function DotPulse() {
  return (
    <span className="inline-flex items-center gap-0.5">
      <span className="dot-pulse-1 h-1 w-1 rounded-full bg-amber-400" />
      <span className="dot-pulse-2 h-1 w-1 rounded-full bg-amber-400" />
      <span className="dot-pulse-3 h-1 w-1 rounded-full bg-amber-400" />
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

