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
  Sparkles,
} from "lucide-react";
import { useMessages } from "@/api/client";
import type { Message, SpawnCardData } from "@/api/types";
import { useUi } from "@/store/ui";
import { filterMessagesByWindow } from "@/panes/instances";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { Badge } from "@/components/ui/badge";
import { cn, shortId } from "@/lib/utils";

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
  const includeMeta = useUi((s) => s.includeMeta);
  const setIncludeMeta = useUi((s) => s.setIncludeMeta);
  const callsOnly = useUi((s) => s.callsOnly);
  const setCallsOnly = useUi((s) => s.setCallsOnly);
  const focusedPane = useUi((s) => s.focusedPane);

  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (focusedPane !== "thread") return;
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      const typing =
        target &&
        (target.tagName === "INPUT" ||
          target.tagName === "TEXTAREA" ||
          target.isContentEditable);
      if (typing) return;
      if (
        e.key !== "ArrowUp" &&
        e.key !== "ArrowDown" &&
        e.key !== "PageUp" &&
        e.key !== "PageDown"
      )
        return;
      const root = scrollRef.current;
      if (!root) return;
      const el = root.querySelector(
        "[data-radix-scroll-area-viewport]",
      ) as HTMLElement | null;
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
    windowOverride && (windowOverride.start || windowOverride.end)
      ? windowOverride
      : null;
  return (
    <Shell>
      <header className="flex items-center justify-between border-b border-border px-3 py-2">
        <div>
          <div className="text-[11px] uppercase tracking-wider text-muted-foreground">
            trace
          </div>
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <span>{shortId(sessionId)}</span>
            {windowed ? (
              <Badge
                variant="outline"
                className="border-amber-500/40 text-[9px] uppercase text-amber-300"
                title={`window: ${windowed.start ?? "(begin)"} – ${windowed.end ?? "(open)"}`}
              >
                window {fmtTime(windowed.start)} – {fmtTime(windowed.end)}
              </Badge>
            ) : null}
          </div>
        </div>
        <div className="flex items-center gap-3">
          <label
            className="flex items-center gap-1.5 text-[11px] text-muted-foreground"
            title="hide messages and tool use, show only call structure"
          >
            <input
              type="checkbox"
              checked={callsOnly}
              onChange={(e) => setCallsOnly(e.target.checked)}
              className="h-3 w-3"
            />
            calls only
          </label>
          <label className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
            <input
              type="checkbox"
              checked={includeMeta}
              onChange={(e) => setIncludeMeta(e.target.checked)}
              className="h-3 w-3"
            />
            meta
          </label>
        </div>
      </header>
      <ScrollArea ref={scrollRef} className="flex-1">
        <div className="min-w-0 px-4 py-3">
          <SessionTrace
            slug={slug!}
            sessionId={sessionId}
            depth={0}
            includeMeta={includeMeta}
            callsOnly={callsOnly}
            window={windowed}
          />
        </div>
      </ScrollArea>
    </Shell>
  );
}

function fmtTime(iso: string | null): string {
  if (!iso) return "(open)";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "?" : d.toLocaleTimeString();
}

function Shell({ children }: { children: React.ReactNode }) {
  return <div className="flex h-full flex-col">{children}</div>;
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
  callsOnly,
  autoOpen = false,
  window: traceWindow = null,
}: {
  slug: string;
  sessionId: string;
  depth: number;
  includeMeta: boolean;
  callsOnly: boolean;
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
      messages: filterMessagesByWindow(
        data.messages,
        traceWindow.start,
        traceWindow.end,
      ),
      // ``extra_spawns`` have no per-window anchor — pin them to the latest
      // (open-ended) window so resume-bounded slices don't repeat them.
      extra_spawns: traceWindow.end === null ? data.extra_spawns ?? [] : [],
    };
  }, [data, traceWindow]);

  const groups = useMemo(
    () => (windowed ? groupMessages(windowed.messages) : []),
    [windowed],
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

  const visibleItems = useMemo(() => {
    if (!callsOnly) return items;
    return items.filter(
      (it) =>
        it.kind === "extra" ||
        (it.kind === "group" &&
          it.group.kind === "tool" &&
          it.group.toolUse.spawn_kind != null),
    );
  }, [items, callsOnly]);

  if (isLoading) {
    return (
      <div className="text-xs text-muted-foreground">
        loading {shortId(sessionId)}…
      </div>
    );
  }
  if (error) {
    return (
      <div className="text-xs text-destructive">
        {(error as Error).message}
      </div>
    );
  }
  if (!data) return null;

  return (
    <div className="space-y-3">
      {visibleItems.map((it, i) =>
        it.kind === "group" ? (
          <Group
            key={i}
            group={it.group}
            slug={slug}
            depth={depth}
            includeMeta={includeMeta}
            callsOnly={callsOnly}
            autoOpen={autoOpen}
          />
        ) : (
          <ExtraSpawnCard
            key={i}
            spawn={it.spawn}
            slug={slug}
            depth={depth}
            includeMeta={includeMeta}
            callsOnly={callsOnly}
            autoOpen={autoOpen}
          />
        ),
      )}
      {visibleItems.length === 0 && (
        <div className="text-xs italic text-muted-foreground">
          {callsOnly ? "no callstack/subagent calls in this session." : "no messages."}
        </div>
      )}
    </div>
  );
}

// --- grouping ----------------------------------------------------------------

type RenderGroup =
  | { kind: "msg"; msg: Message }
  | { kind: "tool"; toolUse: Message; toolResult?: Message };

type OrderedItem =
  | { kind: "group"; group: RenderGroup; ts: number }
  | { kind: "extra"; spawn: SpawnCardData; ts: number };


function groupMessages(messages: Message[]): RenderGroup[] {
  const out: RenderGroup[] = [];
  const pending = new Map<string, number>();
  for (const m of messages) {
    if (m.role === "tool_use") {
      const g: RenderGroup = { kind: "tool", toolUse: m };
      out.push(g);
      if (m.tool_use_id) pending.set(m.tool_use_id, out.length - 1);
    } else if (m.role === "tool_result") {
      const id = m.tool_result_for;
      if (id && pending.has(id)) {
        const idx = pending.get(id)!;
        const g = out[idx];
        if (g.kind === "tool") {
          g.toolResult = m;
          pending.delete(id);
        }
      } else {
        out.push({ kind: "msg", msg: m });
      }
    } else {
      out.push({ kind: "msg", msg: m });
    }
  }
  return out;
}

// --- group renderer ----------------------------------------------------------

function Group({
  group,
  slug,
  depth,
  includeMeta,
  callsOnly,
  autoOpen,
}: {
  group: RenderGroup;
  slug: string;
  depth: number;
  includeMeta: boolean;
  callsOnly: boolean;
  autoOpen: boolean;
}) {
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
        callsOnly={callsOnly}
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
  callsOnly,
  autoOpen,
}: {
  toolUse: Message;
  toolResult?: Message;
  slug: string;
  depth: number;
  includeMeta: boolean;
  callsOnly: boolean;
  autoOpen: boolean;
}) {
  const isCall = toolUse.spawn_kind === "call";
  const children = toolUse.spawn_session_ids ?? [];
  const tasks = perChildTasks(toolUse, children.length);
  const overallStatus: "pending" | "ok" | "error" = !toolResult
    ? "pending"
    : toolResult.is_error
      ? "error"
      : "ok";

  if (children.length === 0) {
    // No children resolved — fall through to a single placeholder row.
    return (
      <SpawnRow
        isCall={isCall}
        title={summarizeCallTarget(toolUse)}
        childId={null}
        status={overallStatus}
        timestamp={toolUse.timestamp}
        slug={slug}
        depth={depth}
        includeMeta={includeMeta}
        callsOnly={callsOnly}
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
          title={tasks[i] ?? summarizeCallTarget(toolUse)}
          childId={childId}
          status={overallStatus}
          timestamp={toolUse.timestamp}
          slug={slug}
          depth={depth}
          includeMeta={includeMeta}
          callsOnly={callsOnly}
          autoOpen={autoOpen}
        />
      ))}
    </div>
  );
}

function perChildTasks(m: Message, childCount: number): string[] {
  const input = m.tool_input as Record<string, unknown> | null;
  if (input && typeof input === "object" && Array.isArray((input as Record<string, unknown>).tasks)) {
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
  callsOnly,
  autoOpen,
}: {
  spawn: SpawnCardData;
  slug: string;
  depth: number;
  includeMeta: boolean;
  callsOnly: boolean;
  autoOpen: boolean;
}) {
  const status: "pending" | "ok" | "error" =
    spawn.status === "running" || spawn.status === "in_progress"
      ? "pending"
      : spawn.status === "failed" || spawn.status === "error"
        ? "error"
        : "ok";

  if (spawn.children.length === 0) return null;

  return (
    <div className="space-y-2">
      {spawn.children.map((childId, i) => (
        <SpawnRow
          key={childId}
          isCall
          title={spawn.tasks[i] ?? `child ${i + 1}`}
          childId={childId}
          status={status}
          timestamp={spawn.started_at}
          slug={slug}
          depth={depth}
          includeMeta={includeMeta}
          callsOnly={callsOnly}
          autoOpen={autoOpen}
        />
      ))}
    </div>
  );
}

// --- single-child spawn row -------------------------------------------------

function SpawnRow({
  isCall,
  title,
  childId,
  status,
  timestamp,
  slug,
  depth,
  includeMeta,
  callsOnly,
  autoOpen,
}: {
  isCall: boolean;
  title: string;
  childId: string | null;
  status: "pending" | "ok" | "error";
  timestamp: string | null;
  slug: string;
  depth: number;
  includeMeta: boolean;
  callsOnly: boolean;
  autoOpen: boolean;
}) {
  const [open, setOpen] = useState(autoOpen);
  const accent = isCall
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
    <Collapsible
      open={open && expandable}
      onOpenChange={(v) => expandable && setOpen(v)}
    >
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
          className={cn(
            "uppercase text-[10px]",
            accent.pillBorder,
            accent.pillText,
          )}
        >
          {accent.label}
        </Badge>
        <span className="min-w-0 flex-1 truncate font-mono text-[12px] text-foreground">
          {title}
        </span>
        {status === "pending" && <Badge variant="warn">live</Badge>}
        {status === "error" && (
          <span className="inline-flex items-center gap-1 text-[11px] text-red-400">
            <XCircle className="h-3 w-3" />
            error
          </span>
        )}
        {status === "ok" && (
          <CheckCircle2 className="h-3 w-3 text-emerald-400" />
        )}
        {shortChildId ? (
          <span className="font-mono text-[10px] text-muted-foreground">
            {shortChildId}
          </span>
        ) : (
          <span className="text-[10px] italic text-muted-foreground">
            resolving…
          </span>
        )}
        {timestamp ? (
          <span className="text-[10px] text-muted-foreground tabular-nums">
            {new Date(timestamp).toLocaleTimeString()}
          </span>
        ) : null}
      </CollapsibleTrigger>
      {expandable && (
        <CollapsibleContent>
          <div
            className={cn(
              "ml-3 mt-2 space-y-3 border-l-2 pl-4",
              accent.railBorder,
            )}
          >
            <SessionTrace
              slug={slug}
              sessionId={childId!}
              depth={depth + 1}
              includeMeta={includeMeta}
              callsOnly={callsOnly}
              autoOpen={callsOnly}
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

function MessageBubble({ msg }: { msg: Message }) {
  const isUser = msg.role === "user";
  const isAssistant = msg.role === "assistant";
  const isSystem = msg.role === "system" || msg.role === "tool_result";

  return (
    <div className="flex gap-3">
      <div className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full border border-border bg-muted">
        {isUser && <User className="h-3.5 w-3.5" />}
        {isAssistant && <Bot className="h-3.5 w-3.5" />}
        {isSystem && <Info className="h-3.5 w-3.5" />}
      </div>
      <div className="min-w-0 flex-1">
        <div className="mb-1 flex items-center gap-2 text-[10px] uppercase tracking-wider text-muted-foreground">
          <span>{msg.role}</span>
          {msg.model ? (
            <span className="font-mono normal-case">{msg.model}</span>
          ) : null}
          {msg.timestamp ? (
            <span className="tabular-nums normal-case">
              {new Date(msg.timestamp).toLocaleTimeString()}
            </span>
          ) : null}
        </div>
        <div
          className={cn(
            "rounded-lg border border-border bg-card px-3 py-2",
            isSystem && "bg-muted/40",
          )}
        >
          {msg.text ? (
            <div className="uw-markdown">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>
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
      </div>
    </div>
  );
}

function ToolCard({
  toolUse,
  toolResult,
}: {
  toolUse: Message;
  toolResult?: Message;
}) {
  const status: "pending" | "ok" | "error" = !toolResult
    ? "pending"
    : toolResult.is_error
      ? "error"
      : "ok";

  return (
    <div className="flex gap-3">
      <div className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full border border-border bg-muted">
        <Wrench className="h-3.5 w-3.5" />
      </div>
      <div className="min-w-0 flex-1">
        <div className="mb-1 flex items-center gap-2 text-[10px] uppercase tracking-wider text-muted-foreground">
          <span>tool</span>
          <span className="font-mono text-foreground normal-case">
            {toolUse.tool_name ?? "?"}
          </span>
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
            <span className="flex-1 truncate text-xs font-mono">
              {summarizeInput(toolUse.tool_input)}
            </span>
          </CollapsibleTrigger>
          <CollapsibleContent>
            <div className="mt-1 space-y-2 rounded-lg border border-border bg-card/70 px-3 py-2">
              <section>
                <div className="mb-1 text-[10px] uppercase tracking-wider text-muted-foreground">
                  input
                </div>
                <pre className="overflow-x-auto whitespace-pre-wrap break-words text-xs font-mono">
                  {JSON.stringify(toolUse.tool_input, null, 2)}
                </pre>
              </section>
              {toolResult ? (
                <section>
                  <div className="mb-1 flex items-center gap-2 text-[10px] uppercase tracking-wider text-muted-foreground">
                    result
                    {toolResult.is_error && (
                      <Badge variant="error">error</Badge>
                    )}
                  </div>
                  <pre className="overflow-x-auto whitespace-pre-wrap break-words text-xs font-mono">
                    {stringifyResult(toolResult.tool_result)}
                  </pre>
                </section>
              ) : (
                <section>
                  <div className="text-[11px] italic text-muted-foreground">
                    awaiting result…
                  </div>
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

function summarizeInput(input: unknown): string {
  if (input == null) return "(no input)";
  if (typeof input === "string") return input;
  if (typeof input === "object") {
    const obj = input as Record<string, unknown>;
    const parts: string[] = [];
    for (const k of Object.keys(obj).slice(0, 3)) {
      const v = obj[k];
      const s =
        typeof v === "string"
          ? v
          : typeof v === "number" || typeof v === "boolean"
            ? String(v)
            : JSON.stringify(v);
      parts.push(`${k}=${truncate(s, 80)}`);
    }
    return parts.join(" · ");
  }
  return String(input);
}

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

function truncate(s: string, n: number) {
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

export { ChevronDown };
