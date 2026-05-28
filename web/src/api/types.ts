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
  // True when this tool_use *references* an already-running invocation
  // (``await_call`` polling a background call's invoke_id) rather than
  // spawning a new one. The window-assignment in CompactCard PEEKS the
  // first matching canvas child for follower rows; the originating
  // ``call`` row already popped the window, and a second pop would
  // leave the await row anchored to nothing. Defaults to false on
  // older messages so the existing single-row behaviour is unchanged.
  spawn_is_follower?: boolean;
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
  /** Extra source → target window edges for ``await_call`` rows. The
   *  standard ``parent_window_id`` edge anchors the originating
   *  ``call`` row; a follower edge anchors the matching ``await_call``
   *  row to the same child window so the connection is visible. The
   *  frontend assembles the ReactFlow ``sourceHandle`` from
   *  ``parent_tool_use_id``; ``target_window_id`` is the child window
   *  the await polls (resolved server-side by ``invoke_id``). */
  follower_edges: { parent_tool_use_id: string; target_window_id: string }[];
  children: WindowNode[];
};

export type CanvasTreeResponse = {
  root: WindowNode;
  all_windows: WindowNode[];
};

// --- Usage report ------------------------------------------------------

/** One project's totals for the report window. Same shape as
 *  ``ProjectGroupRow`` minus the ``project_count`` / ``label`` so the
 *  Reports table can render top-N rows and bucket rows with one
 *  component. */
export type ProjectUsageRow = {
  slug: string;
  source_path: string;
  session_count: number;
  usage: TokenUsage;
  cost: TokenCost;
  total_tokens: number;
  total_cost: number;
};

/** A rolled-up bucket — ``ephemeral`` (synthetic ``/tmp`` test runs) or
 *  ``other`` (the long tail past top-N). Several projects collapsed
 *  into one row. */
export type ProjectGroupRow = {
  label: string;
  project_count: number;
  session_count: number;
  usage: TokenUsage;
  cost: TokenCost;
  total_tokens: number;
  total_cost: number;
};

export type UsageBuckets = {
  /** Top-N real projects, already sorted by ``total_cost`` desc. */
  top: ProjectUsageRow[];
  /** Aggregate of every project flagged as ``ephemeral`` (synthetic
   *  ``/tmp`` scaffolds). ``null`` when there were none in the window. */
  ephemeral: ProjectGroupRow | null;
  /** Aggregate of every real project past the top-N cut. ``null`` when
   *  there are no extra real projects. */
  other: ProjectGroupRow | null;
};

export type UsageReportResponse = {
  /** ``YYYY-MM`` — the local calendar month this report covers. */
  month: string;
  /** Informational timezone label (e.g. ``"PDT"``). Shown in the report
   *  footer so users understand which clock the month boundary uses. */
  tz_name: string;
  window_start_utc: string;
  window_end_utc: string;
  /** Number of projects with at least one in-window event. */
  project_count: number;
  /** Number of sessions with at least one in-window event. */
  session_count: number;
  /** Sum of every in-window event's tokens — includes ephemerals and
   *  the tail. The grand total card uses this directly. */
  grand_usage: TokenUsage;
  /** USD cost matching ``grand_usage``, priced per-event at the
   *  recording model's rate. */
  grand_cost: TokenCost;
  total_tokens: number;
  total_cost: number;
  ephemeral_path_prefixes: string[];
  ephemeral_slug_prefixes: string[];
  buckets: UsageBuckets;
  /** Every project with in-window activity, sorted by cost. The Reports
   *  view uses ``buckets`` for its default layout; this list is here
   *  for views that want to re-bucket or page beyond ``top``. */
  projects: ProjectUsageRow[];
};
