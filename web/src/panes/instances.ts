/** Helpers for filtering a session's message stream to a window slice
 *  and for labelling resume rows.
 *
 *  This used to also house ``windowsForParent`` and the spawn-edge types
 *  used by the old incremental canvas tree builder; those moved into
 *  the backend (``unwind.canvas_tree``) once the canvas switched to
 *  consuming a server-precomputed tree.
 */

import type { Message } from "@/api/types";


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
