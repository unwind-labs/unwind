/** A canvas node is a ``(sessionId, [windowStart, windowEnd))`` tuple — a
 *  Claude session viewed through the lens of a single time range. The root
 *  node has ``[null, null)`` (the whole session). Each ``invoke`` /
 *  ``invoke_resume`` of the same child session produces another node viewing
 *  a different slice. ``windowEnd === null`` means open-ended ("still
 *  running / latest"); a non-null ``windowEnd`` means a later resume of the
 *  same session bounds this slice. */

import type { Message } from "@/api/types";

export type SpawnEdgeInfo = {
  parent: string;
  child: string;
  /** Underlying Claude session id for this child. May repeat across siblings
   *  when a single session is invoked + resumed; the (parent, handleId) tuple
   *  is what's globally unique. */
  childSessionId: string;
  handleId: string;
  spawnKind: "call" | "subagent";
  done: boolean;
  /** Per-handle display label (already includes the "↻" resume prefix when
   *  ``isResume`` is true). */
  label: string;
  /** Parent-side tool_use timestamp (ISO). Used to order multiple windows
   *  of the same child session and compute their boundaries. ``null`` when
   *  the parent's tool_use lacks a timestamp (rare; treat as start-of-time). */
  parentToolUseTs: string | null;
  /** True for ``invoke_resume`` rows — captures the semantic that the parent
   *  re-entered a previously-yielded session, distinct from the structural
   *  fact that ``windowStart > 0``. Drives the "↻ resumed" pill. */
  isResume: boolean;
  /** Optional resume-specific user reply (used as label fallback). */
  userReply?: string;
};

export type SessionWindow = {
  /** Unique node id (= handleId). */
  nodeId: string;
  parentNodeId: string;
  sessionId: string;
  /** ISO timestamp inclusive — start of this window. */
  windowStart: string | null;
  /** ISO timestamp exclusive — ``null`` means open-ended (latest). */
  windowEnd: string | null;
  /** True when this window came from ``invoke_resume`` (or is the second-or-
   *  later window of a session under one parent). */
  isResume: boolean;
  done: boolean;
  spawnKind: "call" | "subagent";
};

/** Compute the per-window slices of one parent's child sessions.
 *
 *  Spawns whose tool_use_id couldn't be resolved (childSessionId empty) are
 *  ignored — they aren't on the canvas yet.
 *
 *  Within a (parent, sessionId) group, windows are ordered by
 *  ``parentToolUseTs`` ascending. Window ``i`` owns ``[ts(i), ts(i+1))``;
 *  the last window's ``windowEnd`` is ``null``. */
export function windowsForParent(
  parentNodeId: string,
  spawns: SpawnEdgeInfo[],
): SessionWindow[] {
  // Group by sessionId so we can sort timestamps within each session.
  const bySession: Record<string, SpawnEdgeInfo[]> = {};
  for (const sp of spawns) {
    if (!sp.childSessionId) continue;
    (bySession[sp.childSessionId] ??= []).push(sp);
  }

  const out: SessionWindow[] = [];
  for (const sid of Object.keys(bySession)) {
    const group = bySession[sid].slice().sort((a, b) => {
      const at = a.parentToolUseTs ? Date.parse(a.parentToolUseTs) : 0;
      const bt = b.parentToolUseTs ? Date.parse(b.parentToolUseTs) : 0;
      return at - bt;
    });
    group.forEach((sp, i) => {
      const next = group[i + 1];
      out.push({
        nodeId: sp.handleId,
        parentNodeId,
        sessionId: sid,
        windowStart: sp.parentToolUseTs,
        windowEnd: next ? next.parentToolUseTs : null,
        isResume: sp.isResume || i > 0,
        done: sp.done,
        spawnKind: sp.spawnKind,
      });
    });
  }
  return out;
}

/** Filter a child session's messages to a window ``[start, end)``.
 *
 *  ``start === null`` means "from the beginning"; ``end === null`` means
 *  "to the end". Inherited messages (``is_inherited``) are kept since they
 *  predate any invocation and the activity-bucket logic handles them. */
export function filterMessagesByWindow(
  messages: Message[],
  start: string | null,
  end: string | null,
): Message[] {
  if (start == null && end == null) return messages;
  const startMs = start ? Date.parse(start) : -Infinity;
  const endMs = end ? Date.parse(end) : Infinity;
  return messages.filter((m) => {
    if (!m.timestamp) {
      // No timestamp — include if instance is the first AND only (covers
      // inherited / system rows). When start is set, drop them rather than
      // double-counting across instances.
      return start == null;
    }
    const t = Date.parse(m.timestamp);
    return t >= startMs && t < endMs;
  });
}

/** Stable label fallback for an ``invoke_resume`` row whose tool_input has no
 *  ``task`` / ``tasks`` field but does have ``user_reply``. The card renders
 *  a "resumed" glyph next to the label, so this returns plain text — no
 *  decorative prefix. */
export function labelForResume(userReply: string | null | undefined): string {
  const r = (userReply ?? "").trim();
  if (!r) return "(resumed)";
  if (r.length <= 40) return r;
  return `${r.slice(0, 40).trimEnd()}…`;
}
