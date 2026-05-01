export type SessionStatus = "live" | "idle" | "done";

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
  role: "user" | "assistant" | "tool_use" | "tool_result" | "system";
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
  origin_session_id: string | null;
  is_inherited: boolean;
  spawn_kind: "call" | "subagent" | null;
  spawn_session_ids: string[];
  spawn_tasks: string[];
  // Per-child completion derived from the callstack task status. Lets the
  // caller card check off finished children individually before the parent
  // ``invoke_parallel`` tool_result lands. ``null`` means unknown (fall
  // back to the parent's tool_result).
  spawn_done?: (boolean | null)[];
};

export type AncestorRef = {
  session_id: string;
  title: string | null;
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
  ancestors: AncestorRef[];
  extra_spawns: SpawnCardData[];
};
