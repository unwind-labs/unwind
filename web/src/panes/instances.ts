/** Helpers for filtering a session's message stream to a window slice
 *  and for labelling resume rows.
 *
 *  This used to also house ``windowsForParent`` and the spawn-edge types
 *  used by the old incremental canvas tree builder; those moved into
 *  the backend (``unwind.canvas_tree``) once the canvas switched to
 *  consuming a server-precomputed tree.
 */

import type { Message, SpawnCardData } from "@/api/types";

/** Filter ``extra_spawns`` to a single window ``[start, end)``.
 *
 *  Extras represent callstack-Skill spawns without an MCP tool_use anchor,
 *  so they're sliced by ``started_at`` rather than by message uuid order.
 *  Extras with no ``started_at`` (legacy / fork-fallback) pin to whichever
 *  window is the open-ended latest one. */
export function filterExtrasByWindow(
  extras: SpawnCardData[] | null | undefined,
  start: string | null,
  end: string | null,
): SpawnCardData[] {
  if (!extras) return [];
  const isLatest = end == null;
  const startMs = start ? Date.parse(start) : -Infinity;
  const endMs = end ? Date.parse(end) : Infinity;
  return extras.filter((s) => {
    if (!s.started_at) return isLatest;
    const t = Date.parse(s.started_at);
    return t >= startMs && t < endMs;
  });
}

/** Filter a child session's messages to a window ``[start, end)``.
 *
 *  ``start === null`` means "from the beginning"; ``end === null`` means
 *  "to the end".
 *
 *  Half-open boundary contract — pinned by ``instances.test.ts``:
 *  - A message whose timestamp equals ``start`` is INCLUDED.
 *  - A message whose timestamp equals ``end`` is EXCLUDED — it belongs to
 *    the next window. Adjacent windows therefore tile without overlap and
 *    without gaps; every timestamped message lands in exactly one window.
 *    This matches how ``windowEnd`` is set (= the next ``invoke_resume``'s
 *    ``tool_use`` timestamp, which is itself a message that belongs to the
 *    resumed instance).
 *
 *  Cross-window relationships — DO NOT widen this filter:
 *  Consumers that need to relate a ``tool_use`` in window N to its
 *  ``tool_result`` in window N+1 (e.g. flipping a spawn row's ``done``
 *  badge, or showing the result text in a trace pane) must NOT rely on
 *  finding both messages inside one windowed slice. They are expected to
 *  consult the full unwindowed stream or a server-precomputed summary:
 *    • ``deriveRows`` takes a third ``allMessages`` arg for exactly this
 *      reason — the spawn-row canonical status is resolved against the
 *      full stream, so a result that landed in a later window still
 *      flips the row in the window where the call originated.
 *    • ``SpawnCard`` reads ``spawn_status`` from the canvas tree (which
 *      the server computes across all windows) for the per-row status
 *      icon, so the indicator is correct even if the boundary
 *      ``tool_result`` message is invisible in the prior window's trace.
 *  Switching to inclusive end would silently double-count in activity
 *  buckets and duplicate boundary rows across adjacent trace panes. */
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

/** A flat sequence of items the trace pane renders: either a standalone
 *  message bubble, or a ``tool_use`` paired with its ``tool_result``. */
export type RenderGroup =
  | { kind: "msg"; msg: Message }
  | { kind: "tool"; toolUse: Message; toolResult?: Message };

/** Group a session's messages for trace-pane rendering, pairing each
 *  ``tool_use`` with its matching ``tool_result``.
 *
 *  ``allMessages`` (optional): the full unwindowed message stream. When the
 *  trace pane is showing a windowed slice, a child's ``tool_result``
 *  timestamp can collide with the next ``invoke_resume``'s ``tool_use``
 *  timestamp — both land on the same millisecond, and the half-open
 *  ``[start, end)`` filter drops the result into the NEXT window. The
 *  originating ``tool_use`` is then orphaned in the current window and the
 *  SpawnCard / ToolCard renders ``awaiting result…`` even though the result
 *  exists.
 *
 *  When ``allMessages`` is supplied, any ``tool_use`` whose result is missing
 *  from the windowed slice borrows the result by ``tool_use_id`` from the
 *  full stream. The borrowed result is NOT inserted as its own group — it's
 *  still part of the next window's stream and renders there normally.
 *
 *  Omitting ``allMessages`` preserves the legacy single-arg behavior used
 *  for nested (per-child) traces, which are not windowed. */
export function groupMessages(messages: Message[], allMessages?: Message[]): RenderGroup[] {
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
  if (allMessages && pending.size > 0) {
    // Borrow boundary-orphan tool_results from the full stream. Build the
    // lookup lazily — pending.size > 0 already gated us in.
    const resultsById = new Map<string, Message>();
    for (const m of allMessages) {
      if (m.role !== "tool_result") continue;
      const id = m.tool_result_for;
      if (id && !resultsById.has(id)) resultsById.set(id, m);
    }
    for (const [id, idx] of pending) {
      const tr = resultsById.get(id);
      if (!tr) continue;
      const g = out[idx];
      if (g.kind === "tool") g.toolResult = tr;
    }
  }
  return out;
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
