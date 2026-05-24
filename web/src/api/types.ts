export type SessionStatus = "live" | "yield" | "idle" | "done";

export type SessionRow = {
  session_id: string;
  title: string;
  custom_title: string | null;
  first_timestamp: string | null;
  last_timestamp: string | null;
  message_count: number;
  top_level_call_count: number;
  cwd: string | null;
  git_branch: string | null;
  status: SessionStatus;
};

export type ProjectSummary = {
  slug: string;
  source_path: string;
  last_activity: string | null;
  session_count: number;
};

export type DefaultProject = {
  slug: string | null;
  source_path: string | null;
};

export type TaskKind = "call" | "subagent";

/** Sub-classification of a "call" spawn — drives the per-row icon.
 *  Only meaningful when ``spawn_kind === "call"``. */
export type CallType = "fork" | "fresh" | "fresh_cross_project";

export type TaskNode = {
  session_id: string | null;
  task: string;
  status: string;
  depth: number;
  duration_seconds: number | null;
  summary: string | null;
  error: string | null;
  invoke_id: string | null;
  started_at: string | null;
  ended_at: string | null;
  kind: TaskKind;
  children: TaskNode[];
};

export type TreeResponse = {
  session_id: string;
  children: TaskNode[];
  has_callstack_logs: boolean;
};

export type Message = {
  uuid: string;
  session_id: string;
  role: "user" | "assistant" | "thinking" | "tool_use" | "tool_result" | "system";
  timestamp: string | null;
  text: string | null;
  tool_name: string | null;
  tool_input: unknown;
  tool_use_id: string | null;
  tool_result_for: string | null;
  tool_result: unknown;
  is_error: boolean;
  model: string | null;
  raw_type: string | null;
  spawn_kind: "call" | "subagent" | null;
  spawn_session_ids: string[];
  spawn_tasks: string[];
  // Per-child completion derived from the callstack task status. Lets the
  // caller card check off finished children individually before the parent
  // ``invoke_parallel`` tool_result lands. ``null`` means unknown (fall
  // back to the parent's tool_result).
  spawn_done?: (boolean | null)[];
  // Per-child call type (parallel to spawn_session_ids). Drives the icon
  // Unwind renders per spawn row. Older messages without this field fall
  // back to "fork" so the UI still renders.
  spawn_call_types?: CallType[];
};

export type SpawnCardData = {
  invoke_id: string;
  started_at: string | null;
  ended_at: string | null;
  status: string;
  children: string[];
  tasks: string[];
};

export type MessagesResponse = {
  session_id: string;
  messages: Message[];
  last_uuid: string | null;
  file_offset: number;
  extra_spawns: SpawnCardData[];
};

/** Token usage counters mirroring Anthropic's ``message.usage`` shape:
 *    cw = cache_creation_input_tokens
 *    cr = cache_read_input_tokens
 *    r  = input_tokens
 *    w  = output_tokens
 */
export type TokenUsage = {
  cw: number;
  cr: number;
  r: number;
  w: number;
};

/** USD cost matching ``TokenUsage`` — same keys, but each value is the
 *  dollar cost of those tokens at the recording assistant message's
 *  model rate. Computed server-side so the frontend never embeds rate
 *  tables. */
export type TokenCost = {
  cw: number;
  cr: number;
  r: number;
  w: number;
};

/** One slice of a session's activity = one card on the canvas. */
export type WindowNode = {
  window_id: string;
  session_id: string;
  label: string;
  window_start: string | null;
  window_end: string | null;
  status: "done" | "live" | "yield" | "failed";
  /** Max-priority status across this window and every descendant
   *  (``live`` > ``yield`` > ``failed`` > ``done``). Lets the rail on
   *  an otherwise-finished ancestor reflect "work is still happening
   *  somewhere below". */
  subtree_status: "done" | "live" | "yield" | "failed";
  kind: "root" | "call" | "subagent" | "resume";
  parent_window_id: string | null;
  window_index: number;
  /** Tokens attributed to this window alone. */
  self_usage: TokenUsage;
  /** Tokens for this window plus every descendant. Equals ``self_usage``
   *  on leaves; the footer renders a single row there. */
  subtree_usage: TokenUsage;
  /** USD cost attributed to this window alone (priced per-record at the
   *  recording model's rate). */
  self_cost: TokenCost;
  /** USD cost for this window plus every descendant. The root card uses
   *  this for the ``$`` footer row + grand total. */
  subtree_cost: TokenCost;
  children: WindowNode[];
};

export type CanvasTreeResponse = {
  root: WindowNode;
  all_windows: WindowNode[];
};
