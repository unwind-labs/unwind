import { useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  ChevronDown,
  ChevronRight,
  User,
  Bot,
  Wrench,
  CheckCircle2,
  XCircle,
  Info,
  GitFork,
  Hourglass,
  Sparkles,
  Activity,
  Copy,
  Check,
} from "lucide-react";
import { useMessages } from "@/api/client";
import type { Message, SpawnCardData } from "@/api/types";
import type { Status } from "@/lib/status";
import { useUi, type TraceMode } from "@/store/ui";
import {
  filterExtrasByWindow,
  filterMessagesByWindow,
  groupMessages,
  type RenderGroup,
} from "@/panes/instances";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Badge } from "@/components/ui/badge";
import { cn, shortId } from "@/lib/utils";
import { isTypingTarget } from "@/lib/keyboard";
import { describeSystem, describeTool, lineDiff, type DiffRow } from "@/panes/message-renderers";

/** Strip dangerous link schemes from markdown content.
 *
 *  Trace messages can contain arbitrary user-provided text, including
 *  assistant output that may include links. ReactMarkdown's default
 *  ``urlTransform`` already drops some schemes, but we want a stricter
 *  allow-list: only http(s), mailto, and same-document anchors. Anything
 *  else (``javascript:``, ``data:``, custom schemes) becomes the literal
 *  text ``#`` so a click is a no-op rather than an XSS vector. */
function safeUrlTransform(url: string): string {
  const trimmed = url.trim().toLowerCase();
  if (trimmed.startsWith("http:") || trimmed.startsWith("https:")) return url;
  if (trimmed.startsWith("mailto:")) return url;
  if (trimmed.startsWith("#") || trimmed.startsWith("/")) return url;
  return "#";
}

/**
 * Single-pane content view: renders a session's full message trace, where
 * callstack/Agent tool_use blocks expand inline into the spawned child's
 * trace, indented and recursive. A "calls only" toggle hides regular
 * messages so the call structure stands out.
 */
export type TraceWindow = { start: string | null; end: string | null };

export function TracePane({
  sessionIdOverride,
  windowOverride,
}: {
  sessionIdOverride?: string;
  /** When set, the trace shows only messages whose timestamp falls in
   *  ``[start, end)``. Used by the canvas detail overlay so a windowed
   *  node opens only the slice of the session it represents. */
  windowOverride?: TraceWindow | null;
} = {}) {
  const slug = useUi((s) => s.slug);
  const rootSessionId = useUi((s) => s.rootSessionId);
  const sessionId = sessionIdOverride ?? rootSessionId;
  const traceMode = useUi((s) => s.traceMode);
  const setTraceMode = useUi((s) => s.setTraceMode);
  const focusedPane = useUi((s) => s.focusedPane);

  // ``detailed`` and ``raw`` both want the complete record set (metadata
  // included); ``compact`` collapses non-call turns into activity lines.
  const includeMeta = traceMode === "detailed" || traceMode === "raw";
  const compact = traceMode === "compact";

  const scrollRef = useRef<HTMLDivElement>(null);

  // Full model id for the header summary, read off the most recent
  // assistant turn. Same useMessages cache key as the depth-0
  // SessionTrace below, so this is a cache read, not a second fetch.
  const { data: traceData } = useMessages(slug, sessionId, includeMeta);
  const headerModel = useMemo(() => {
    const msgs = traceData?.messages;
    if (!msgs) return null;
    for (let i = msgs.length - 1; i >= 0; i--) if (msgs[i].model) return msgs[i].model;
    return null;
  }, [traceData]);

  useEffect(() => {
    if (focusedPane !== "thread") return;
    const onKey = (e: KeyboardEvent) => {
      if (isTypingTarget(e)) return;
      if (
        e.key !== "ArrowUp" &&
        e.key !== "ArrowDown" &&
        e.key !== "PageUp" &&
        e.key !== "PageDown"
      )
        return;
      const root = scrollRef.current;
      if (!root) return;
      const el = root.querySelector("[data-radix-scroll-area-viewport]") as HTMLElement | null;
      if (!el) return;
      e.preventDefault();
      const dy =
        e.key === "PageDown"
          ? el.clientHeight - 40
          : e.key === "PageUp"
            ? -(el.clientHeight - 40)
            : e.key === "ArrowDown"
              ? 40
              : -40;
      el.scrollBy({ top: dy, behavior: "auto" });
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [focusedPane]);

  if (!sessionId) {
    return (
      <Shell>
        <div className="flex h-full items-center justify-center text-xs text-muted-foreground">
          select a session on the left.
        </div>
      </Shell>
    );
  }

  const windowed =
    windowOverride && (windowOverride.start || windowOverride.end) ? windowOverride : null;
  return (
    <Shell>
      <header className="flex items-center justify-between border-b border-border px-3 py-2">
        <div>
          <div className="text-[11px] uppercase tracking-wider text-muted-foreground">trace</div>
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <span className="font-mono">{sessionId}</span>
            {headerModel ? (
              <Badge
                variant="outline"
                className="border-border/60 font-mono text-[9px] normal-case text-muted-foreground"
              >
                {headerModel}
              </Badge>
            ) : null}
            {windowed ? (
              <Badge
                variant="outline"
                className="border-amber-500/40 text-[9px] uppercase text-amber-300"
                title={`window: ${windowed.start ?? "(begin)"} – ${windowed.end ?? "(open)"}`}
              >
                {fmtTime(windowed.start) ?? "start"} – {fmtTime(windowed.end) ?? "now"}
              </Badge>
            ) : null}
          </div>
        </div>
        <ViewModeToolbar mode={traceMode} onChange={setTraceMode} />
      </header>
      <ScrollArea ref={scrollRef} className="flex-1">
        <div className="min-w-0 px-4 py-3">
          {traceMode === "raw" ? (
            <RawTrace slug={slug!} sessionId={sessionId} window={windowed} />
          ) : (
            <SessionTrace
              slug={slug!}
              sessionId={sessionId}
              depth={0}
              includeMeta={includeMeta}
              compact={compact}
              window={windowed}
            />
          )}
        </div>
      </ScrollArea>
    </Shell>
  );
}

function fmtTime(iso: string | null): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "?" : d.toLocaleTimeString();
}

function Shell({ children }: { children: React.ReactNode }) {
  return <div className="flex h-full flex-col">{children}</div>;
}

const VIEW_MODES: { value: TraceMode; label: string; title: string }[] = [
  { value: "compact", label: "compact", title: "call structure with intermediate message counts" },
  { value: "normal", label: "normal", title: "full message trace" },
  { value: "detailed", label: "detailed", title: "full message trace including metadata" },
  { value: "raw", label: "raw", title: "underlying records as copyable JSONL" },
];

/** Segmented control selecting how the trace below renders. */
function ViewModeToolbar({
  mode,
  onChange,
}: {
  mode: TraceMode;
  onChange: (m: TraceMode) => void;
}) {
  return (
    <div className="flex items-center gap-0.5 rounded-md border border-border bg-muted/30 p-0.5">
      {VIEW_MODES.map((m) => (
        <button
          key={m.value}
          type="button"
          title={m.title}
          aria-pressed={mode === m.value}
          onClick={() => onChange(m.value)}
          className={cn(
            "rounded px-2 py-1 text-[11px] lowercase transition-colors",
            mode === m.value
              ? "bg-background text-foreground shadow-sm"
              : "text-muted-foreground hover:text-foreground",
          )}
        >
          {m.label}
        </button>
      ))}
    </div>
  );
}

/** Raw view: every record in the (windowed) session shown as pretty-printed
 *  JSON for readability, with a one-click copy that yields newline-delimited
 *  JSONL — one compact object per line — so it round-trips back into tooling. */
function RawTrace({
  slug,
  sessionId,
  window: traceWindow,
}: {
  slug: string;
  sessionId: string;
  window?: TraceWindow | null;
}) {
  const { data, isLoading, error } = useMessages(slug, sessionId, true);
  const [copied, setCopied] = useState(false);

  const messages = useMemo(() => {
    if (!data) return [];
    if (!traceWindow || (!traceWindow.start && !traceWindow.end)) return data.messages;
    return filterMessagesByWindow(data.messages, traceWindow.start, traceWindow.end);
  }, [data, traceWindow]);

  const jsonl = useMemo(() => messages.map((m) => JSON.stringify(m)).join("\n"), [messages]);
  const pretty = useMemo(
    () => messages.map((m) => JSON.stringify(m, null, 2)).join("\n"),
    [messages],
  );

  const onCopy = () => {
    void navigator.clipboard.writeText(jsonl).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  };

  if (isLoading) {
    return <div className="text-xs text-muted-foreground">loading {shortId(sessionId)}…</div>;
  }
  if (error) {
    return <div className="text-xs text-destructive">{(error as Error).message}</div>;
  }
  if (messages.length === 0) {
    return <div className="text-xs italic text-muted-foreground">no records.</div>;
  }

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <span className="text-[10px] uppercase tracking-wider text-muted-foreground">
          {messages.length} records · jsonl
        </span>
        <button
          type="button"
          onClick={onCopy}
          className="inline-flex items-center gap-1.5 rounded-md border border-border bg-card px-2 py-1 text-[11px] text-muted-foreground transition-colors hover:bg-accent/40 hover:text-foreground"
        >
          {copied ? <Check className="h-3 w-3 text-emerald-400" /> : <Copy className="h-3 w-3" />}
          {copied ? "copied" : "copy as jsonl"}
        </button>
      </div>
      <pre className="overflow-x-auto whitespace-pre rounded-lg border border-border bg-card px-3 py-2 text-[11px] font-mono leading-relaxed">
        {pretty}
      </pre>
    </div>
  );
}

/**
 * Render a session's trace: messages + inline spawn cards (anchored on
 * tool_use) + extra spawn cards (callstack-spawned children that don't
 * have a tool_use anchor). Used recursively when a spawn card expands.
 */
function SessionTrace({
  slug,
  sessionId,
  depth,
  includeMeta,
  compact,
  autoOpen = false,
  window: traceWindow = null,
}: {
  slug: string;
  sessionId: string;
  depth: number;
  includeMeta: boolean;
  /** Compact view: drop regular messages/tool cards and collapse each run
   *  of them into a single "N msgs" activity line between the call rows
   *  (mirrors the canvas node card). */
  compact: boolean;
  /** If true, default-expand any spawn cards so the user sees the whole sub-tree. */
  autoOpen?: boolean;
  /** When set, filter this session's messages to ``[start, end)``. Only
   *  applied at the OUTER call (depth 0) — recursive renders for spawned
   *  children always show the child's full session. */
  window?: TraceWindow | null;
}) {
  const { data, isLoading, error } = useMessages(slug, sessionId, includeMeta);

  // Apply the window only at the outermost render; nested spawn expansions
  // call SessionTrace recursively and should show the child's full trace.
  const windowed = useMemo(() => {
    if (!data) return null;
    if (!traceWindow || (!traceWindow.start && !traceWindow.end)) return data;
    return {
      ...data,
      messages: filterMessagesByWindow(data.messages, traceWindow.start, traceWindow.end),
      extra_spawns: filterExtrasByWindow(data.extra_spawns, traceWindow.start, traceWindow.end),
    };
  }, [data, traceWindow]);

  // Pass the FULL unwindowed stream (data.messages) as the second arg so a
  // tool_use whose tool_result fell into the next window (boundary collision
  // on the half-open ``[start, end)`` filter) still pairs up here. Without
  // this, expanding the SpawnCard / ToolCard would show ``awaiting result…``
  // even after the call returned. See ``groupMessages`` JSDoc.
  const groups = useMemo<RenderGroup[]>(
    () => (windowed ? groupMessages(windowed.messages, data?.messages) : []),
    [windowed, data],
  );

  // Place extra spawn cards immediately after the last assistant message in
  // this session — that's where the JSON envelope was emitted that callstack
  // parsed to fire the children. Callstack's report timestamps inherit from
  // the outer invoke, so we can't trust them for in-session positioning.
  const items = useMemo<OrderedItem[]>(() => {
    if (!windowed) return [];
    const result: OrderedItem[] = [];
    const extras = windowed.extra_spawns ?? [];
    if (extras.length === 0) {
      return groups.map((g) => ({ kind: "group", group: g, ts: 0 }));
    }
    // Find the index of the last assistant-role group; extras go right after.
    let lastAssistantIdx = -1;
    for (let i = groups.length - 1; i >= 0; i--) {
      const g = groups[i];
      const role = g.kind === "msg" ? g.msg.role : g.toolUse.role;
      if (role === "assistant" || role === "tool_use") {
        lastAssistantIdx = i;
        break;
      }
    }
    const insertAfter = lastAssistantIdx >= 0 ? lastAssistantIdx : groups.length - 1;
    for (let i = 0; i < groups.length; i++) {
      result.push({ kind: "group", group: groups[i], ts: 0 });
      if (i === insertAfter) {
        for (const s of extras) {
          result.push({ kind: "extra", spawn: s, ts: 0 });
        }
      }
    }
    if (groups.length === 0) {
      for (const s of extras) result.push({ kind: "extra", spawn: s, ts: 0 });
    }
    return result;
  }, [windowed, groups]);

  // Compact: keep spawn rows (anchored tool_use + extras) and collapse each
  // maximal run of the remaining messages/tool cards into one activity line
  // carrying the count and time span — the same summary the canvas card shows.
  const visibleItems = useMemo(() => {
    if (!compact) return items;
    const out: OrderedItem[] = [];
    let count = 0;
    let firstTs: string | null = null;
    let lastTs: string | null = null;
    const flush = () => {
      if (count === 0) return;
      const span =
        firstTs && lastTs ? Math.max(0, (Date.parse(lastTs) - Date.parse(firstTs)) / 1000) : 0;
      out.push({ kind: "activity", count, spanSeconds: span, ts: 0 });
      count = 0;
      firstTs = null;
      lastTs = null;
    };
    for (const it of items) {
      const isSpawn =
        it.kind === "extra" ||
        (it.kind === "group" && it.group.kind === "tool" && it.group.toolUse.spawn_kind != null);
      if (isSpawn) {
        flush();
        out.push(it);
        continue;
      }
      // it.kind === "group" non-spawn: one logical event (tool_use+result
      // already paired into a single group), so count it as one.
      count += 1;
      if (it.kind === "group") {
        const ts = it.group.kind === "msg" ? it.group.msg.timestamp : it.group.toolUse.timestamp;
        if (ts) {
          if (!firstTs) firstTs = ts;
          lastTs = ts;
        }
      }
    }
    flush();
    return out;
  }, [items, compact]);

  if (isLoading) {
    return <div className="text-xs text-muted-foreground">loading {shortId(sessionId)}…</div>;
  }
  if (error) {
    return <div className="text-xs text-destructive">{(error as Error).message}</div>;
  }
  if (!data) return null;

  // The parent's call runtime classifies a child's outcome AFTER the
  // child exits — so the failure verdict (e.g. "child emitted no
  // parseable envelope") never appears in the child's own JSONL.
  // Surface it here as a banner so the detail view matches the card's
  // red-X terminator on the canvas. ``terminal_status`` is canonical
  // (``done|live|yield|failed``); a single equality check is enough.
  const isFailedTerminal = data.terminal_status === "failed";

  return (
    <div className="space-y-3">
      {isFailedTerminal && depth === 0 && (
        <div className="flex items-start gap-2 rounded border border-red-500/40 bg-red-500/10 px-3 py-2 text-[12px] text-red-200">
          <XCircle className="mt-0.5 h-4 w-4 shrink-0 text-red-400" />
          <div className="min-w-0 flex-1">
            <div className="text-[10px] font-bold uppercase tracking-[0.18em] text-red-300">
              session ended with error
            </div>
            {data.terminal_error && (
              <div className="mt-0.5 break-words font-mono text-[11px] text-red-100/90">
                {data.terminal_error}
              </div>
            )}
          </div>
        </div>
      )}
      {visibleItems.map((it, i) =>
        it.kind === "group" ? (
          <Group
            key={i}
            group={it.group}
            slug={slug}
            depth={depth}
            includeMeta={includeMeta}
            compact={compact}
            autoOpen={autoOpen}
          />
        ) : it.kind === "extra" ? (
          <ExtraSpawnCard
            key={i}
            spawn={it.spawn}
            slug={slug}
            depth={depth}
            includeMeta={includeMeta}
            compact={compact}
            autoOpen={autoOpen}
          />
        ) : (
          <ActivityRow key={i} count={it.count} spanSeconds={it.spanSeconds} />
        ),
      )}
      {visibleItems.length === 0 && (
        <div className="text-xs italic text-muted-foreground">
          {compact ? "no activity in this session." : "no messages."}
        </div>
      )}
    </div>
  );
}

// --- grouping ----------------------------------------------------------------

type OrderedItem =
  | { kind: "group"; group: RenderGroup; ts: number }
  | { kind: "extra"; spawn: SpawnCardData; ts: number }
  | { kind: "activity"; count: number; spanSeconds: number; ts: number };

/** Intermediate "N msgs" line shown between call rows in compact view —
 *  the trace-pane analogue of the canvas card's activity row. */
function ActivityRow({ count, spanSeconds }: { count: number; spanSeconds: number }) {
  return (
    <div className="flex items-center gap-2 pl-1 text-[11px] text-muted-foreground">
      <Activity className="h-3 w-3 opacity-60" />
      <span>activity</span>
      <span className="opacity-60">·</span>
      <span>
        {count} msg{count === 1 ? "" : "s"}
      </span>
      {spanSeconds > 0 ? (
        <>
          <span className="opacity-60">·</span>
          <span className="tabular-nums">{formatSpan(spanSeconds)}</span>
        </>
      ) : null}
    </div>
  );
}

function formatSpan(s: number): string {
  if (s < 1) return `${Math.round(s * 1000)}ms`;
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return `${m}m ${sec}s`;
}

// --- group renderer ----------------------------------------------------------

function Group({
  group,
  slug,
  depth,
  includeMeta,
  compact,
  autoOpen,
}: {
  group: RenderGroup;
  slug: string;
  depth: number;
  includeMeta: boolean;
  compact: boolean;
  autoOpen: boolean;
}) {
  // Tool/system cards always render their rich, type-aware chrome (normal
  // and detailed alike). ``includeMeta`` only governs whether metadata/system
  // records are fetched in the first place, not how they're drawn.
  if (group.kind === "msg") {
    return <MessageBubble msg={group.msg} />;
  }
  if (group.toolUse.spawn_kind) {
    return (
      <SpawnCard
        toolUse={group.toolUse}
        toolResult={group.toolResult}
        slug={slug}
        depth={depth}
        includeMeta={includeMeta}
        compact={compact}
        autoOpen={autoOpen}
      />
    );
  }
  return <ToolCard toolUse={group.toolUse} toolResult={group.toolResult} />;
}

// --- spawn card (anchored to a tool_use) ------------------------------------

function SpawnCard({
  toolUse,
  toolResult,
  slug,
  depth,
  includeMeta,
  compact,
  autoOpen,
}: {
  toolUse: Message;
  toolResult?: Message;
  slug: string;
  depth: number;
  includeMeta: boolean;
  compact: boolean;
  autoOpen: boolean;
}) {
  const isCall = toolUse.spawn_kind === "call";
  const isFollower = toolUse.spawn_is_follower === true;
  const children = toolUse.spawn_session_ids ?? [];
  const tasks = perChildTasks(toolUse, children.length);

  // Per-child canonical status. ``spawn_status`` (set server-side from
  // the callstack report via ``status_for_spawn``) is the authoritative
  // per-child outcome and wins when known — it correctly reflects "the
  // spawn finished" even when the parent's ``tool_result`` envelope
  // isn't visible in the current window slice (the trace pane's window
  // filter is exclusive on the end boundary, so a tool_result whose
  // timestamp matches ``window_end`` exactly gets dropped; without
  // this fallback, the row reads as ``live`` long after the child
  // returned). Falls back to the tool_result-arrival heuristic, which
  // can only distinguish ok vs error vs pending.
  const statusFor = (i: number): Status | null => {
    const s = toolUse.spawn_status?.[i];
    if (s != null) return s;
    if (!toolResult) return null;
    return toolResult.is_error ? "failed" : "done";
  };

  if (children.length === 0) {
    // No children resolved — fall through to a single placeholder row.
    return (
      <SpawnRow
        isCall={isCall}
        isFollower={isFollower}
        title={summarizeCallTarget(toolUse)}
        childId={null}
        status={statusFor(0)}
        timestamp={toolUse.timestamp}
        slug={slug}
        depth={depth}
        includeMeta={includeMeta}
        compact={compact}
        autoOpen={autoOpen}
      />
    );
  }

  return (
    <div className="space-y-2">
      {children.map((childId, i) => (
        <SpawnRow
          key={childId}
          isCall={isCall}
          isFollower={isFollower}
          title={tasks[i] ?? summarizeCallTarget(toolUse)}
          childId={childId}
          status={statusFor(i)}
          timestamp={toolUse.timestamp}
          slug={slug}
          depth={depth}
          includeMeta={includeMeta}
          compact={compact}
          autoOpen={autoOpen}
        />
      ))}
    </div>
  );
}

function perChildTasks(m: Message, childCount: number): string[] {
  const input = m.tool_input as Record<string, unknown> | null;
  if (
    input &&
    typeof input === "object" &&
    Array.isArray((input as Record<string, unknown>).tasks)
  ) {
    const tasks = (input as { tasks: unknown[] }).tasks.map(String);
    if (tasks.length === childCount) return tasks;
  }
  // Fallback: same task label for every child.
  const label =
    typeof (input as Record<string, unknown> | null)?.task === "string"
      ? ((input as Record<string, unknown>).task as string)
      : typeof (input as Record<string, unknown> | null)?.description === "string"
        ? ((input as Record<string, unknown>).description as string)
        : (m.tool_name ?? "call");
  return Array(childCount).fill(label);
}

// --- spawn card (extras — derived from callstack reports) -------------------

function ExtraSpawnCard({
  spawn,
  slug,
  depth,
  includeMeta,
  compact,
  autoOpen,
}: {
  spawn: SpawnCardData;
  slug: string;
  depth: number;
  includeMeta: boolean;
  compact: boolean;
  autoOpen: boolean;
}) {
  // ``spawn.status`` is already canonical (set server-side via
  // ``status_for_spawn``). The frontend doesn't translate raw
  // report.yaml strings anymore — that was the bug that let extras
  // and anchored CALL rows show different verdicts for the same
  // child. Same vocabulary, same renderer.
  const status: Status | null = spawn.status;

  if (spawn.children.length === 0) return null;

  return (
    <div className="space-y-2">
      {spawn.children.map((childId, i) => (
        <SpawnRow
          key={childId}
          isCall
          isFollower={false}
          title={spawn.tasks[i] ?? `child ${i + 1}`}
          childId={childId}
          status={status}
          timestamp={spawn.started_at}
          slug={slug}
          depth={depth}
          includeMeta={includeMeta}
          compact={compact}
          autoOpen={autoOpen}
        />
      ))}
    </div>
  );
}

// --- single-child spawn row -------------------------------------------------

function SpawnRow({
  isCall,
  isFollower,
  title,
  childId,
  status,
  timestamp,
  slug,
  depth,
  includeMeta,
  compact,
  autoOpen,
}: {
  isCall: boolean;
  /** ``await_call`` tool_use referencing an already-running invocation
   *  instead of spawning a new one. Renders with the same sky color
   *  family as the originating CALL row but transparent — the original
   *  CALL stays visually dominant. Mirrors the canvas-card styling. */
  isFollower: boolean;
  title: string;
  childId: string | null;
  status: Status | null;
  timestamp: string | null;
  slug: string;
  depth: number;
  includeMeta: boolean;
  compact: boolean;
  autoOpen: boolean;
}) {
  const [open, setOpen] = useState(autoOpen);
  // ``isFollower`` checked first: an await_call has ``spawn_kind ==
  // "call"``, so without this branch it would fall through to the
  // CALL accent. Order: follower > call > subagent.
  const accent = isFollower
    ? {
        leftBorder: "border-l-sky-500",
        ring: "ring-sky-500/30",
        bg: "bg-transparent",
        bgHover: "hover:bg-sky-950/30",
        pillBorder: "border-sky-500/40",
        pillText: "text-sky-300",
        railBorder: "border-l-sky-500/60",
        icon: <Hourglass className="h-3.5 w-3.5 text-sky-300" />,
        label: "await",
      }
    : isCall
      ? {
          leftBorder: "border-l-sky-500",
          ring: "ring-sky-500/30",
          bg: "bg-sky-950/40",
          bgHover: "hover:bg-sky-950/60",
          pillBorder: "border-sky-500/40",
          pillText: "text-sky-300",
          railBorder: "border-l-sky-500/60",
          icon: <GitFork className="h-3.5 w-3.5 text-sky-300" />,
          label: "call",
        }
      : {
          leftBorder: "border-l-violet-500",
          ring: "ring-violet-500/30",
          bg: "bg-violet-950/40",
          bgHover: "hover:bg-violet-950/60",
          pillBorder: "border-violet-500/40",
          pillText: "text-violet-300",
          railBorder: "border-l-violet-500/60",
          icon: <Sparkles className="h-3.5 w-3.5 text-violet-300" />,
          label: "subagent",
        };

  const expandable = childId !== null;
  const shortChildId = childId
    ? childId.startsWith("agent-")
      ? childId.slice(6, 14)
      : childId.slice(0, 8)
    : null;

  return (
    <Collapsible open={open && expandable} onOpenChange={(v) => expandable && setOpen(v)}>
      <CollapsibleTrigger
        disabled={!expandable}
        className={cn(
          "group flex w-full items-center gap-2 rounded-md border border-border border-l-4 px-3 py-2.5 text-left ring-1 ring-transparent transition-all",
          accent.leftBorder,
          accent.bg,
          expandable && accent.bgHover,
          open && expandable && `ring-1 ${accent.ring}`,
          !expandable && "opacity-70",
        )}
      >
        <ChevronRight
          className={cn(
            "h-3.5 w-3.5 shrink-0 text-muted-foreground transition-transform",
            open && expandable && "rotate-90",
            !expandable && "opacity-30",
          )}
        />
        {accent.icon}
        <Badge
          variant="outline"
          className={cn("uppercase text-[10px]", accent.pillBorder, accent.pillText)}
        >
          {accent.label}
        </Badge>
        <span className="min-w-0 flex-1 truncate font-mono text-[12px] text-foreground">
          {title}
        </span>
        {(status === "live" || status == null) && <Badge variant="warn">live</Badge>}
        {status === "yield" && (
          <span className="inline-flex items-center gap-1 text-[11px] text-amber-300">
            <Hourglass className="h-3 w-3" />
            yield
          </span>
        )}
        {status === "failed" && (
          <span className="inline-flex items-center gap-1 text-[11px] text-red-400">
            <XCircle className="h-3 w-3" />
            error
          </span>
        )}
        {status === "done" && <CheckCircle2 className="h-3 w-3 text-emerald-400" />}
        {shortChildId ? (
          <span className="font-mono text-[10px] text-muted-foreground">{shortChildId}</span>
        ) : (
          <span className="text-[10px] italic text-muted-foreground">resolving…</span>
        )}
        {timestamp ? (
          <span className="text-[10px] text-muted-foreground tabular-nums">
            {new Date(timestamp).toLocaleTimeString()}
          </span>
        ) : null}
      </CollapsibleTrigger>
      {expandable && (
        <CollapsibleContent>
          <div className={cn("ml-3 mt-2 space-y-3 border-l-2 pl-4", accent.railBorder)}>
            <SessionTrace
              slug={slug}
              sessionId={childId!}
              depth={depth + 1}
              includeMeta={includeMeta}
              compact={compact}
              autoOpen={compact}
            />
          </div>
        </CollapsibleContent>
      )}
    </Collapsible>
  );
}

function summarizeCallTarget(m: Message): string {
  const input = m.tool_input as Record<string, unknown> | null;
  if (input && typeof input === "object") {
    if (Array.isArray((input as Record<string, unknown>).tasks)) {
      const tasks = (input as { tasks: unknown[] }).tasks;
      return tasks.map(String).join(", ");
    }
    if (typeof (input as Record<string, unknown>).task === "string") {
      return (input as Record<string, unknown>).task as string;
    }
    if (typeof (input as Record<string, unknown>).description === "string") {
      return (input as Record<string, unknown>).description as string;
    }
  }
  return m.tool_name ?? "call";
}

// --- regular message bubbles -------------------------------------------------

// Per-role visual differences live here so MessageBubble below stays a single
// flow. ``collapsible`` wraps header+body in a closed-by-default disclosure
// (currently only ``thinking``, which we hide so chain-of-thought doesn't
// crowd out actual replies). ``bubbleClass`` is appended to the shared body
// classes — ``border-dashed`` + ``italic`` + muted overlay distinguishes
// thinking from a regular assistant turn at a glance.
type BubbleVariant = {
  Icon: React.ComponentType<{ className?: string }>;
  iconClass?: string;
  bubbleClass?: string;
  collapsible?: boolean;
};

const BUBBLE_VARIANT: Record<Message["role"], BubbleVariant> = {
  user: { Icon: User },
  assistant: { Icon: Bot },
  system: { Icon: Info, bubbleClass: "bg-muted/40" },
  tool_result: { Icon: Info, bubbleClass: "bg-muted/40" },
  thinking: {
    Icon: Sparkles,
    iconClass: "text-muted-foreground",
    bubbleClass: "border-dashed bg-muted/30 italic text-muted-foreground",
    collapsible: true,
  },
  tool_use: { Icon: Wrench }, // not normally routed here; ToolCard handles tool_use
};

function MessageBubble({ msg }: { msg: Message }) {
  // System/attachment records (skill listings, hook output, tool/instruction
  // deltas) get a type-aware card instead of a generic muted bubble. These
  // only appear in the detailed view (where metadata is fetched), but the
  // rendering itself isn't gated on the mode.
  if (msg.role === "system") {
    return <SystemCard msg={msg} />;
  }

  // Extended-thinking placeholders ("[redacted thinking]" / "[encrypted
  // thinking]") have no body worth disclosing — render a single dim line.
  if (msg.role === "thinking" && /^\[(redacted|encrypted) thinking\]$/.test(msg.text ?? "")) {
    return (
      <div className="flex items-center gap-2 pl-1 text-[11px] italic text-muted-foreground">
        <Sparkles className="h-3 w-3 opacity-60" />
        {msg.text}
      </div>
    );
  }

  const { Icon, iconClass, bubbleClass, collapsible } = BUBBLE_VARIANT[msg.role];

  const header = (
    <div className="mb-1 flex items-center gap-2 text-[10px] uppercase tracking-wider text-muted-foreground">
      {collapsible ? (
        <ChevronRight className="h-3.5 w-3.5 transition-transform group-data-[state=open]:rotate-90" />
      ) : null}
      <span>{msg.role}</span>
      {msg.model ? <span className="font-mono normal-case">{msg.model}</span> : null}
      {msg.timestamp ? (
        <span className="tabular-nums normal-case">
          {new Date(msg.timestamp).toLocaleTimeString()}
        </span>
      ) : null}
    </div>
  );

  const body = (
    <div className={cn("rounded-lg border border-border bg-card px-3 py-2", bubbleClass)}>
      {msg.text ? (
        <div className="uw-markdown">
          <ReactMarkdown remarkPlugins={[remarkGfm]} urlTransform={safeUrlTransform}>
            {msg.text}
          </ReactMarkdown>
        </div>
      ) : msg.tool_result !== undefined && msg.tool_result !== null ? (
        <pre className="overflow-x-auto whitespace-pre-wrap break-words text-xs font-mono">
          {stringifyResult(msg.tool_result)}
        </pre>
      ) : (
        <span className="text-xs italic text-muted-foreground">(empty)</span>
      )}
    </div>
  );

  return (
    <div className="flex gap-3">
      <div className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full border border-border bg-muted">
        <Icon className={cn("h-3.5 w-3.5", iconClass)} />
      </div>
      <div className="min-w-0 flex-1">
        {collapsible ? (
          <Collapsible defaultOpen={false}>
            <CollapsibleTrigger className="group block w-full text-left hover:text-foreground">
              {header}
            </CollapsibleTrigger>
            <CollapsibleContent>{body}</CollapsibleContent>
          </Collapsible>
        ) : (
          <>
            {header}
            {body}
          </>
        )}
      </div>
    </div>
  );
}

/** Type-aware card for ``role: "system"`` attachment records in the detailed
 *  view — skill listings, hook output, MCP instruction/tool deltas, etc. The
 *  ``raw_type`` (attachment subtype) drives the icon, label and trailing
 *  detail; the body collapses closed since these are usually long. */
function SystemCard({ msg }: { msg: Message }) {
  const { Icon, label, detail, body, tone, markdown } = describeSystem(msg);
  const accent = tone === "error" ? "text-red-400" : "text-muted-foreground";
  return (
    <div className="flex gap-3">
      <div className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full border border-border bg-muted">
        <Icon className={cn("h-3.5 w-3.5", accent)} />
      </div>
      <div className="min-w-0 flex-1">
        <Collapsible defaultOpen={false}>
          <CollapsibleTrigger className="group flex w-full items-center gap-2 rounded-lg border border-border bg-muted/40 px-3 py-2 text-left hover:bg-accent/40">
            <ChevronRight className="h-3.5 w-3.5 shrink-0 text-muted-foreground transition-transform group-data-[state=open]:rotate-90" />
            <span
              className={cn(
                "text-[10px] font-bold uppercase tracking-[0.16em]",
                tone === "error" ? "text-red-300" : "text-foreground/80",
              )}
            >
              {label}
            </span>
            {detail ? (
              <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-muted-foreground">
                {detail}
              </span>
            ) : (
              <span className="flex-1" />
            )}
            {msg.timestamp ? (
              <span className="text-[10px] tabular-nums text-muted-foreground">
                {new Date(msg.timestamp).toLocaleTimeString()}
              </span>
            ) : null}
          </CollapsibleTrigger>
          <CollapsibleContent>
            <div className="mt-1 rounded-lg border border-border bg-card/70 px-3 py-2">
              {!body ? (
                <span className="text-xs italic text-muted-foreground">(empty)</span>
              ) : markdown ? (
                <div className="uw-markdown text-xs">
                  <ReactMarkdown remarkPlugins={[remarkGfm]} urlTransform={safeUrlTransform}>
                    {body}
                  </ReactMarkdown>
                </div>
              ) : (
                <pre className="overflow-x-auto whitespace-pre-wrap break-words text-xs font-mono text-muted-foreground">
                  {body}
                </pre>
              )}
            </div>
          </CollapsibleContent>
        </Collapsible>
      </div>
    </div>
  );
}

/** Extract the before/after line diffs for an Edit (one) or MultiEdit (one
 *  per edit). Returns ``null`` for any other tool so ToolCard falls back to
 *  the raw input JSON. */
function editDiffs(toolUse: Message): DiffRow[][] | null {
  const input = toolUse.tool_input as Record<string, unknown> | null;
  if (!input || typeof input !== "object") return null;
  const diffOf = (e: Record<string, unknown>): DiffRow[] | null =>
    typeof e.old_string === "string" && typeof e.new_string === "string"
      ? lineDiff(e.old_string, e.new_string)
      : null;
  if (toolUse.tool_name === "Edit") {
    const d = diffOf(input);
    return d ? [d] : null;
  }
  if (toolUse.tool_name === "MultiEdit" && Array.isArray(input.edits)) {
    const out: DiffRow[][] = [];
    for (const e of input.edits) {
      if (e && typeof e === "object") {
        const d = diffOf(e as Record<string, unknown>);
        if (d) out.push(d);
      }
    }
    return out.length ? out : null;
  }
  return null;
}

/** Render one before/after diff as +/− lines with green/red accents. */
function EditDiff({ rows }: { rows: DiffRow[] }) {
  return (
    <pre className="overflow-x-auto rounded border border-border bg-background/40 text-xs font-mono leading-relaxed">
      {rows.map((r, i) => (
        <div
          key={i}
          className={cn(
            "whitespace-pre-wrap break-words px-2",
            r.type === "add" && "bg-emerald-950/40 text-emerald-200",
            r.type === "del" && "bg-red-950/40 text-red-200",
            r.type === "ctx" && "text-muted-foreground",
          )}
        >
          <span className="select-none opacity-50">
            {r.type === "add" ? "+ " : r.type === "del" ? "- " : "  "}
          </span>
          {r.text || " "}
        </div>
      ))}
    </pre>
  );
}

function ToolCard({ toolUse, toolResult }: { toolUse: Message; toolResult?: Message }) {
  const status: "pending" | "ok" | "error" = !toolResult
    ? "pending"
    : toolResult.is_error
      ? "error"
      : "ok";

  // Type-aware icon, label and skim line (e.g. "Read TracePane.tsx · 935
  // lines · 28 KB"), shown in every non-compact view.
  const view = describeTool(toolUse, toolResult ? stringifyResult(toolResult.tool_result) : null);
  const HeaderIcon = view.Icon;
  // Edit/MultiEdit get a rendered before/after diff in the body instead of
  // the raw old_string/new_string JSON blob.
  const edits = editDiffs(toolUse);

  return (
    <div className="flex gap-3">
      <div className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full border border-border bg-muted">
        <HeaderIcon className="h-3.5 w-3.5" />
      </div>
      <div className="min-w-0 flex-1">
        <div className="mb-1 flex items-center gap-2 text-[10px] uppercase tracking-wider text-muted-foreground">
          <span>{view.label}</span>
          {status === "pending" && <Badge variant="warn">pending</Badge>}
          {status === "ok" && (
            <span className="inline-flex items-center gap-1 text-emerald-400 normal-case">
              <CheckCircle2 className="h-3 w-3" />
              ok
            </span>
          )}
          {status === "error" && (
            <span className="inline-flex items-center gap-1 text-red-400 normal-case">
              <XCircle className="h-3 w-3" />
              error
            </span>
          )}
          {toolUse.timestamp ? (
            <span className="tabular-nums normal-case">
              {new Date(toolUse.timestamp).toLocaleTimeString()}
            </span>
          ) : null}
        </div>
        <Collapsible defaultOpen={false}>
          <CollapsibleTrigger className="group flex w-full items-center gap-2 rounded-lg border border-border bg-card px-3 py-2 text-left hover:bg-accent/40">
            <ChevronRight className="h-3.5 w-3.5 text-muted-foreground transition-transform group-data-[state=open]:rotate-90" />
            <span className="flex-1 truncate text-xs font-mono">{view.summary}</span>
          </CollapsibleTrigger>
          <CollapsibleContent>
            <div className="mt-1 space-y-2 rounded-lg border border-border bg-card/70 px-3 py-2">
              <section>
                <div className="mb-1 text-[10px] uppercase tracking-wider text-muted-foreground">
                  {edits ? "changes" : "input"}
                </div>
                {edits ? (
                  <div className="space-y-2">
                    {edits.map((e, i) => (
                      <EditDiff key={i} rows={e} />
                    ))}
                  </div>
                ) : (
                  <pre className="overflow-x-auto whitespace-pre-wrap break-words text-xs font-mono">
                    {JSON.stringify(toolUse.tool_input, null, 2)}
                  </pre>
                )}
              </section>
              {toolResult ? (
                <section>
                  <div className="mb-1 flex items-center gap-2 text-[10px] uppercase tracking-wider text-muted-foreground">
                    result
                    {toolResult.is_error && <Badge variant="error">error</Badge>}
                  </div>
                  <pre className="overflow-x-auto whitespace-pre-wrap break-words text-xs font-mono">
                    {stringifyResult(toolResult.tool_result)}
                  </pre>
                </section>
              ) : (
                <section>
                  <div className="text-[11px] italic text-muted-foreground">awaiting result…</div>
                </section>
              )}
            </div>
          </CollapsibleContent>
        </Collapsible>
      </div>
    </div>
  );
}

// --- helpers -----------------------------------------------------------------

function stringifyResult(r: unknown): string {
  if (r == null) return "";
  if (typeof r === "string") return r;
  if (Array.isArray(r)) {
    const parts = r.map((block) => {
      if (block && typeof block === "object") {
        const b = block as Record<string, unknown>;
        if (b.type === "text" && typeof b.text === "string") return b.text;
      }
      return JSON.stringify(block, null, 2);
    });
    return parts.join("\n");
  }
  return JSON.stringify(r, null, 2);
}

export { ChevronDown };
