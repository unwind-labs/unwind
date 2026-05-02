/** Pure derivation of a session's compact-card rows from its message stream.
 *  Lives in its own module (no React imports) so vitest can exercise it
 *  without spinning up a DOM environment. */

import type { Message, SpawnCardData as ExtraSpawn } from "@/api/types";
import { labelForResume } from "./instances";

export const INVOKE_RESUME_TOOL = "mcp__plugin_callstack_call__invoke_resume";

/** A single row inside a compact session card. ONE row per child — for
 *  invoke_parallel with N children we emit N spawn rows so each child has
 *  its own anchor. */
export type Row =
  | { kind: "activity"; count: number; spanSeconds: number }
  | {
      kind: "spawn";
      spawnKind: "call" | "subagent";
      title: string;
      /** Underlying Claude session id of the child. Empty while resolving. */
      childId: string;
      done: boolean;
      handleId: string;
      /** Parent-side ``tool_use`` timestamp. Used by the canvas to order
       *  multiple invocations of the same child session and partition the
       *  child's activity into per-instance time windows. */
      parentToolUseTs: string | null;
      /** True for ``invoke_resume`` — the row represents a re-entry into a
       *  previously-yielded child session. */
      isResume: boolean;
      /** Optional ``user_reply`` from invoke_resume; surfaces as the row
       *  title and instance label when present. */
      userReply?: string;
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
      const isResume = m.tool_name === INVOKE_RESUME_TOOL;
      const userReply = isResume ? extractUserReply(m) : undefined;
      const callDone =
        m.tool_use_id !== null && seenResultFor.has(m.tool_use_id ?? "");
      const labels =
        m.spawn_tasks && m.spawn_tasks.length === m.spawn_session_ids.length
          ? m.spawn_tasks
          : m.spawn_session_ids.map((_, i) => labelFromInput(m, i));
      m.spawn_session_ids.forEach((childId, i) => {
        // Prefer per-child status from the callstack report (set by the
        // server via spawn_done). Falls back to the parent tool_result's
        // arrival when the report doesn't know yet.
        const perChild = m.spawn_done?.[i];
        const done =
          perChild != null
            ? perChild && childId !== ""
            : callDone && childId !== "";
        const rawLabel = labels[i] || "";
        const title = isResume
          ? labelForResume(userReply ?? rawLabel)
          : rawLabel || childId.slice(0, 8) || "(resolving)";
        out.push({
          kind: "spawn",
          spawnKind: m.spawn_kind!,
          title,
          childId,
          done,
          handleId: `spawn-${tooluse}-${i}`,
          parentToolUseTs: m.timestamp,
          isResume,
          userReply,
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

  // Re-check spawn ``done``: a tool_use's done state depends on whether a
  // tool_result for it exists ANYWHERE in the message list. The above loop
  // sees results in order, so for tool_uses that came BEFORE their result
  // we'd have missed it. Re-walk and patch — but only when we don't have a
  // per-child status from the callstack report (that's authoritative).
  const allResultIds = new Set(
    messages
      .filter((m) => m.role === "tool_result" && m.tool_result_for)
      .map((m) => m.tool_result_for!),
  );
  const perChildByHandle: Record<string, boolean | null | undefined> = {};
  for (const m of messages) {
    if (m.role !== "tool_use" || !m.spawn_kind) continue;
    if (!m.spawn_done) continue;
    const tooluse = m.tool_use_id ?? m.uuid;
    m.spawn_done.forEach((d, i) => {
      perChildByHandle[`spawn-${tooluse}-${i}`] = d;
    });
  }
  for (const r of out) {
    if (r.kind === "spawn") {
      const perChild = perChildByHandle[r.handleId];
      if (perChild != null) {
        r.done = perChild && r.childId !== "";
        continue;
      }
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
  //
  // ``handleId`` MUST be globally unique: ReactFlow keys nodes by handleId,
  // so two extras-anchored children that happen to share an ``(ei, i)``
  // pair across different parents would collapse into one node. Use the
  // child's session id (a UUID) as the disambiguator — guaranteed unique
  // and stable across renders.
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
        handleId: `extra-${childId || `${ei}-${i}`}`,
        parentToolUseTs: s.started_at ?? null,
        isResume: false,
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

function extractUserReply(m: Message): string | undefined {
  const input = m.tool_input as Record<string, unknown> | null;
  if (input && typeof input === "object") {
    const r = (input as { user_reply?: unknown }).user_reply;
    if (typeof r === "string") return r;
  }
  return undefined;
}
