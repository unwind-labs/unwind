/** Per-type summary descriptors for the detailed trace view.
 *
 *  These turn a raw ``tool_use`` (paired with its result) or a ``system``
 *  attachment message into a glanceable one-liner — "Read TracePane.tsx ·
 *  935 lines · 28 KB", "Hook · SessionStart:startup" — so the detailed view
 *  is skimmable without expanding every card. Pure functions (no React state)
 *  so they're trivially unit-testable; they return a lucide icon component
 *  plus strings, and the caller (TracePane) owns the actual JSX chrome. */

import {
  FileText,
  Terminal,
  Pencil,
  FilePlus,
  FolderSearch,
  Search,
  ListTodo,
  Globe,
  Sparkles,
  Wrench,
  Webhook,
  Plug,
  ShieldCheck,
  HelpCircle,
  ClipboardList,
  Info,
} from "lucide-react";
import type { Message } from "@/api/types";

type Icon = React.ComponentType<{ className?: string }>;

export type ToolView = {
  Icon: Icon;
  /** Short tool label, e.g. "Read", "Bash", "Edit". */
  label: string;
  /** Skimmable detail line, e.g. "TracePane.tsx · 935 lines · 28 KB". */
  summary: string;
};

export type SystemView = {
  Icon: Icon;
  label: string;
  /** Optional trailing detail shown next to the label (count, hook name, …). */
  detail: string | null;
  /** Cleaned body to render below (prefix already stripped where relevant). */
  body: string;
  /** Render the body as markdown (e.g. the skill listing's "- name: …" lines
   *  become a bulleted list) rather than monospace preformatted text. */
  markdown?: boolean;
  /** Error-toned subtypes (e.g. a failing hook) render with a red accent. */
  tone?: "error";
};

/** One line of a rendered Edit/MultiEdit diff. */
export type DiffRow = { type: "ctx" | "add" | "del"; text: string };

// --- tools -------------------------------------------------------------------

/** Describe a tool_use for the detailed view. ``resultText`` is the
 *  already-stringified tool_result (``null`` while the call is in flight),
 *  used to derive result-side metrics like byte size and line count. */
export function describeTool(toolUse: Message, resultText: string | null): ToolView {
  const name = toolUse.tool_name ?? "tool";
  const input = (toolUse.tool_input ?? {}) as Record<string, unknown>;

  switch (name) {
    case "Read": {
      const file = base(str(input.file_path));
      const from = num(input.offset) ? ` · from line ${num(input.offset)}` : "";
      const detail = resultText
        ? `${countLines(resultText)} lines · ${formatBytes(byteLen(resultText))}`
        : "reading…";
      return { Icon: FileText, label: "Read", summary: `${file}${from} · ${detail}` };
    }
    case "Write": {
      const file = base(str(input.file_path));
      const content = str(input.content ?? input.text ?? input.file_text);
      return {
        Icon: FilePlus,
        label: "Write",
        summary: `${file} · ${countLines(content)} lines · ${formatBytes(byteLen(content))}`,
      };
    }
    case "Edit":
    case "MultiEdit": {
      const file = base(str(input.file_path));
      const added = countLines(str(input.new_string));
      const removed = countLines(str(input.old_string));
      const all = input.replace_all ? " · all" : "";
      return { Icon: Pencil, label: "Edit", summary: `${file} · +${added} −${removed}${all}` };
    }
    case "Bash": {
      const desc = str(input.description);
      const cmd = str(input.command);
      return { Icon: Terminal, label: "Bash", summary: truncate(desc || cmd, 120) };
    }
    case "Glob": {
      const n = resultText ? countLines(resultText) : 0;
      return { Icon: FolderSearch, label: "Glob", summary: `${str(input.pattern)} · ${n} matches` };
    }
    case "Grep": {
      const n = resultText ? countLines(resultText) : 0;
      return { Icon: Search, label: "Grep", summary: `"${str(input.pattern)}" · ${n} lines` };
    }
    case "TodoWrite": {
      const todos = Array.isArray(input.todos) ? input.todos : [];
      const done = todos.filter(
        (t) => (t as Record<string, unknown>)?.status === "completed",
      ).length;
      return { Icon: ListTodo, label: "Todos", summary: `${todos.length} items · ${done} done` };
    }
    case "WebFetch": {
      return { Icon: Globe, label: "Fetch", summary: host(str(input.url)) };
    }
    case "WebSearch": {
      return { Icon: Globe, label: "Search", summary: `"${str(input.query)}"` };
    }
    case "ToolSearch": {
      return { Icon: Search, label: "ToolSearch", summary: truncate(str(input.query), 100) };
    }
    case "Skill": {
      return { Icon: Sparkles, label: "Skill", summary: str(input.skill) };
    }
    case "AskUserQuestion": {
      const qs = Array.isArray(input.questions) ? input.questions : [];
      const first = (qs[0] as Record<string, unknown> | undefined)?.question;
      return {
        Icon: HelpCircle,
        label: "Ask user",
        summary: qs.length > 1 ? `${qs.length} questions` : truncate(str(first), 100) || "question",
      };
    }
    case "ExitPlanMode":
      return { Icon: ClipboardList, label: "Plan", summary: "exit plan mode" };
    default: {
      // MCP tools read as ``mcp__<server>__<tool>`` — show the tool, keep the
      // server as the detail so a wall of ``mcp__…`` prefixes doesn't bury it.
      if (name.startsWith("mcp__")) {
        const parts = name.split("__");
        const tool = parts[parts.length - 1] || name;
        const server = parts[1] || "";
        return {
          Icon: Plug,
          label: tool,
          summary: server ? `via ${server}` : genericSummary(input),
        };
      }
      return { Icon: Wrench, label: name, summary: genericSummary(input) };
    }
  }
}

// --- system / attachment subtypes -------------------------------------------

/** Describe a ``role: "system"`` message by its ``raw_type`` (the attachment
 *  subtype surfaced by the backend). */
export function describeSystem(msg: Message): SystemView {
  const rt = msg.raw_type ?? "system";
  const text = msg.text ?? "";
  switch (rt) {
    case "skill_listing": {
      const count = (text.match(/^\s*-\s/gm) || []).length;
      return {
        Icon: Sparkles,
        label: "Skills available",
        detail: count ? `${count}` : null,
        body: text,
        markdown: true,
      };
    }
    case "hook_success": {
      const { name, rest } = bracketPrefix(text);
      return { Icon: Webhook, label: "Hook", detail: name, body: rest };
    }
    case "hook_non_blocking_error": {
      const { name, rest } = bracketPrefix(text);
      return { Icon: Webhook, label: "Hook error", detail: name, body: rest, tone: "error" };
    }
    case "mcp_instructions_delta":
      return { Icon: Plug, label: "MCP instructions", detail: deltaCount(text), body: text };
    case "deferred_tools_delta":
      return { Icon: Wrench, label: "Tools loaded", detail: deltaCount(text), body: text };
    case "todo_reminder":
      return { Icon: ListTodo, label: "Todo reminder", detail: null, body: text };
    case "command_permissions":
      return { Icon: ShieldCheck, label: "Permissions", detail: null, body: text };
    default:
      return { Icon: Info, label: rt, detail: null, body: text };
  }
}

// --- helpers -----------------------------------------------------------------

function str(v: unknown): string {
  if (typeof v === "string") return v;
  if (v == null) return "";
  return String(v);
}

function num(v: unknown): number {
  return typeof v === "number" ? v : 0;
}

function base(path: string): string {
  if (!path) return "?";
  const parts = path.split("/");
  return parts[parts.length - 1] || path;
}

function host(url: string): string {
  try {
    return new URL(url).host || url;
  } catch {
    return url || "?";
  }
}

/** Count lines in a block. Empty string → 0; otherwise newline-delimited
 *  segments, ignoring a single trailing newline. */
function countLines(s: string): number {
  if (!s) return 0;
  const trimmed = s.endsWith("\n") ? s.slice(0, -1) : s;
  return trimmed.split("\n").length;
}

function byteLen(s: string): number {
  return new TextEncoder().encode(s).length;
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

export function truncate(s: string, n: number): string {
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

/** Compact summary of an arbitrary tool input — first few key=value pairs. */
function genericSummary(input: Record<string, unknown>): string {
  if (!input || typeof input !== "object") return "(no input)";
  const parts: string[] = [];
  for (const k of Object.keys(input).slice(0, 3)) {
    const v = input[k];
    const s =
      typeof v === "string"
        ? v
        : typeof v === "number" || typeof v === "boolean"
          ? String(v)
          : JSON.stringify(v);
    parts.push(`${k}=${truncate(s, 60)}`);
  }
  return parts.join(" · ") || "(no input)";
}

/** Split a ``"[name] body"`` meta string into its parts. */
function bracketPrefix(text: string): { name: string | null; rest: string } {
  const m = text.match(/^\[([^\]]+)\]\s*([\s\S]*)$/);
  return m ? { name: m[1], rest: m[2] } : { name: null, rest: text };
}

/** Line-level diff of two strings via a longest-common-subsequence walk, so
 *  unchanged lines render once as context and only the genuine changes show
 *  as +/−. Sized for Edit/MultiEdit payloads (a handful of lines); the O(n·m)
 *  table is fine there. */
export function lineDiff(oldStr: string, newStr: string): DiffRow[] {
  const a = oldStr.split("\n");
  const b = newStr.split("\n");
  const n = a.length;
  const m = b.length;
  const dp: number[][] = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const rows: DiffRow[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      rows.push({ type: "ctx", text: a[i] });
      i++;
      j++;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      rows.push({ type: "del", text: a[i] });
      i++;
    } else {
      rows.push({ type: "add", text: b[j] });
      j++;
    }
  }
  while (i < n) rows.push({ type: "del", text: a[i++] });
  while (j < m) rows.push({ type: "add", text: b[j++] });
  return rows;
}

/** ``+N −M`` summary from a backend-formatted "added: …\nremoved: …" body. */
function deltaCount(text: string): string | null {
  const added = text.match(/added:\s*(.+)/)?.[1];
  const removed = text.match(/removed:\s*(.+)/)?.[1];
  const ac = added ? added.split(",").length : 0;
  const rc = removed ? removed.split(",").length : 0;
  const parts: string[] = [];
  if (ac) parts.push(`+${ac}`);
  if (rc) parts.push(`−${rc}`);
  return parts.join(" ") || null;
}
