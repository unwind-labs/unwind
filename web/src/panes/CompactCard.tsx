import { useEffect, useMemo, useRef } from "react";
import { Handle, Position, useUpdateNodeInternals } from "reactflow";
import { GitFork, Sparkles, Activity, CheckCircle2 } from "lucide-react";
import { useMessages } from "@/api/client";
import type { Message, SpawnCardData as ExtraSpawn } from "@/api/types";
import { cn, shortId } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";

/** A single row inside a compact session card. ONE row per child — for
 *  invoke_parallel with N children we emit N spawn rows so each child has
 *  its own anchor. */
type Row =
  | { kind: "activity"; count: number; spanSeconds: number }
  | {
      kind: "spawn";
      spawnKind: "call" | "subagent";
      title: string;
      childId: string; // empty string while resolving
      done: boolean;
      handleId: string;
    };

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

export type CompactCardData = {
  slug: string;
  sessionId: string;
  /** Display label — task name from parent if known, else session id prefix. */
  label: string;
  isRoot: boolean;
  selected: boolean;
  /** Called whenever this card discovers spawn rows so the canvas can add child nodes. */
  onSpawnsResolved: (
    cardId: string,
    spawns: { handleId: string; childId: string; label: string; spawnKind: "call" | "subagent"; done: boolean }[],
  ) => void;
  /** Called when the user wants the full session view. */
  onOpenDetail: (sessionId: string) => void;
  /** Called when the rendered card height changes (for dagre re-layout). */
  onMeasure: (sessionId: string, height: number) => void;
  status: "live" | "done";
  /** True when the keyboard cursor is on this card (arrow-key navigation
   *  with the right pane focused). Distinct from `selected`, which means
   *  the detail overlay is currently open for this session. */
  keyboardFocused?: boolean;
};

export function deriveRows(messages: Message[], extras: ExtraSpawn[] = []): Row[] {
  const out: Row[] = [];
  let bucketCount = 0;
  let bucketStart: string | null = null;
  let bucketEnd: string | null = null;

  const flushBucket = () => {
    if (bucketCount === 0) return;
    const span =
      bucketStart && bucketEnd
        ? Math.max(0, (Date.parse(bucketEnd) - Date.parse(bucketStart)) / 1000)
        : 0;
    out.push({ kind: "activity", count: bucketCount, spanSeconds: span });
    bucketCount = 0;
    bucketStart = null;
    bucketEnd = null;
  };

  // Group messages so tool_use and its tool_result are paired (they're not
  // counted as 2 messages in activity buckets — they're one logical event).
  const seenResultFor = new Set<string>();
  for (const m of messages) {
    if (m.role === "tool_result" && m.tool_result_for) {
      seenResultFor.add(m.tool_result_for);
      continue;
    }
    if (m.role === "tool_use" && m.spawn_kind && m.spawn_session_ids?.length) {
      flushBucket();
      const tooluse = m.tool_use_id ?? m.uuid;
      const callDone =
        m.tool_use_id !== null && seenResultFor.has(m.tool_use_id ?? "");
      const labels =
        m.spawn_tasks && m.spawn_tasks.length === m.spawn_session_ids.length
          ? m.spawn_tasks
          : m.spawn_session_ids.map((_, i) => labelFromInput(m, i));
      m.spawn_session_ids.forEach((childId, i) => {
        out.push({
          kind: "spawn",
          spawnKind: m.spawn_kind!,
          title: labels[i] || childId.slice(0, 8) || "(resolving)",
          childId,
          // A child without a session_id is "in flight" and shouldn't render
          // as done until something resolves it.
          done: callDone && childId !== "",
          handleId: `spawn-${tooluse}-${i}`,
        });
      });
      continue;
    }
    bucketCount += 1;
    if (m.timestamp) {
      if (!bucketStart) bucketStart = m.timestamp;
      bucketEnd = m.timestamp;
    }
  }
  flushBucket();

  // Re-check spawn `done`: a tool_use's done state depends on whether a
  // tool_result for it exists ANYWHERE in the message list. The above loop
  // sees results in order, so for tool_uses that came BEFORE their result
  // we'd have missed it. Re-walk and patch.
  const allResultIds = new Set(
    messages
      .filter((m) => m.role === "tool_result" && m.tool_result_for)
      .map((m) => m.tool_result_for!),
  );
  for (const r of out) {
    if (r.kind === "spawn") {
      // handleId is `spawn-<toolUseId>-<i>`; recover toolUseId.
      const m = r.handleId.match(/^spawn-(.+)-\d+$/);
      const toolUseId = m ? m[1] : "";
      const callDone = allResultIds.has(toolUseId);
      r.done = callDone && r.childId !== "";
    }
  }

  // Append extra spawn cards (callstack-derived spawns that don't have a
  // tool_use anchor — e.g. /task-c spawning /task-e/f via callstack:call
  // Skill that emits a JSON envelope instead of an MCP tool call). These
  // sit at the end of the row list since we don't know exactly when they
  // happened relative to messages.
  extras.forEach((s, ei) => {
    s.children.forEach((childId, i) => {
      const callDone = s.status !== "running" && s.status !== "in_progress";
      const taskName = s.tasks[i] ?? `child ${i + 1}`;
      out.push({
        kind: "spawn",
        spawnKind: "call",
        title: taskName || childId.slice(0, 8) || "(call)",
        childId,
        done: callDone && childId !== "",
        handleId: `extra-${ei}-${i}`,
      });
    });
  });

  return out;
}

function labelFromInput(m: Message, i: number): string {
  const input = m.tool_input as Record<string, unknown> | null;
  if (input && typeof input === "object") {
    const tasks = (input as { tasks?: unknown }).tasks;
    if (Array.isArray(tasks) && tasks[i] != null) return String(tasks[i]);
    if (typeof (input as { task?: unknown }).task === "string") {
      return (input as { task: string }).task;
    }
    if (typeof (input as { description?: unknown }).description === "string") {
      return (input as { description: string }).description;
    }
  }
  return m.tool_name ?? "call";
}


export function CompactCardNode({ data }: { data: CompactCardData }) {
  const { data: messages } = useMessages(data.slug, data.sessionId, false);
  const rows: Row[] = messages
    ? deriveRows(messages.messages, messages.extra_spawns ?? [])
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
    updateNodeInternals(data.sessionId);
  }, [handleSignature, data.sessionId, updateNodeInternals]);
  // Status priority:
  //   1. Authoritative session status from canvas (process detection + JSONL
  //      mtime fallback). If "live", trust it — even if no in-flight calls.
  //   2. Otherwise, infer from spawn rows: any unfinished call → live.
  //   3. Otherwise → done.
  const selfStatus: "live" | "done" =
    data.status === "live"
      ? "live"
      : messages && rows.some((r) => r.kind === "spawn" && !r.done)
        ? "live"
        : "done";

  // When spawn rows are discovered, tell the canvas so it can add child cards.
  useEffect(() => {
    if (!messages) return;
    const spawns: {
      handleId: string;
      childId: string;
      label: string;
      spawnKind: "call" | "subagent";
      done: boolean;
    }[] = [];
    for (const r of rows) {
      if (r.kind !== "spawn") continue;
      // Skip rows whose child hasn't resolved yet — no card to draw.
      if (!r.childId) continue;
      spawns.push({
        handleId: r.handleId,
        childId: r.childId,
        label: r.title,
        spawnKind: r.spawnKind,
        done: r.done,
      });
    }
    data.onSpawnsResolved(data.sessionId, spawns);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [messages]);

  // Report measured height up so dagre can re-layout.
  useEffect(() => {
    const el = cardRef.current;
    if (!el) return;
    const obs = new ResizeObserver((entries) => {
      for (const e of entries) {
        data.onMeasure(data.sessionId, e.contentRect.height);
      }
    });
    obs.observe(el);
    return () => obs.disconnect();
  }, [data]);

  // Border color priority (highest wins):
  //   1. selected (detail open)  — Tailwind class chain below.
  //   2. keyboardFocused         — color(srgb 0 0.54 0.8 / 0.8) (sky blue)
  //   3. default border-border.
  // No background changes from hover/focus — only border + shadow.
  const borderColor =
    !data.selected && data.keyboardFocused
      ? "color(srgb 0 0.54 0.8 / 0.8)"
      : undefined;

  return (
    <div
      ref={cardRef}
      className={cn(
        // nopan/nodrag: see comment in CanvasPane — required so d3-zoom
        // doesn't preventDefault on mousedown and break ReactFlow's
        // onNodeClick (which is what restores pointer-events on the
        // wrapper).
        // border-2 is always-on so toggling thickness on hover/focus
        // doesn't shift the card's layout.
        "nopan nodrag overflow-hidden rounded-md border-2 border-border bg-card text-card-foreground shadow-sm transition-[border-color,box-shadow]",
        // Hover: border emphasis + drop shadow, no background change.
        "hover:border-primary/60 hover:shadow-lg",
        // Currently-open state: primary border + subtle fill.
        data.selected && "border-primary bg-primary/10 hover:border-primary",
        selfStatus === "live" && "border-t-emerald-500",
      )}
      style={{
        width: COMPACT_CARD_WIDTH,
        cursor: "pointer",
        ...(borderColor ? { borderColor } : null),
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
      <header className="flex items-center justify-between gap-2 border-b border-border px-3 py-2">
        <div className="min-w-0 flex-1">
          <div className="truncate text-[12px] font-medium text-foreground">
            {data.label}
          </div>
          <div className="font-mono text-[10px] text-muted-foreground">
            {data.isRoot ? "root · " : ""}
            {data.sessionId.startsWith("agent-")
              ? data.sessionId.slice(6, 14)
              : shortId(data.sessionId)}
          </div>
        </div>
        {selfStatus === "live" ? (
          <Badge variant="warn">live</Badge>
        ) : (
          <CheckCircle2 className="h-3 w-3 text-emerald-500/70" />
        )}
      </header>
      <ul className="flex flex-col gap-1 p-2">
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

