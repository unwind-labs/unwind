/** Canonical status vocabulary, shared between every UI surface that
 *  asks "what's the state of this thing?".
 *
 *  Mirrors the backend's ``unwind.status.Status`` literal. Priority is
 *  ``live > yield > failed > done`` — see ``src/unwind/status.py``.
 *
 *  Before this module existed, status interpretation was scattered:
 *  ``derive-rows.ts`` and ``TracePane.tsx`` each string-compared
 *  ``"running" | "in_progress"`` against ``SpawnCard.status``; the
 *  CALL-row icon was a bool projection that collapsed
 *  failed/yielded/done into one "check". Same entity, three different
 *  UI verdicts. Route every status decision through this file so the
 *  next reader has one place to look. */
export type Status = "done" | "live" | "yield" | "failed";

/** True when the entity is no longer running, regardless of outcome.
 *  Used by CALL rows to decide between "in flight" affordances (pulse
 *  dots, click-to-resume) vs settled ones (check / X / sleep icons).
 *  ``null`` is treated as live — the parent's report.yaml hasn't told
 *  us yet, so the conservative default is "assume still in flight"
 *  (otherwise rows flash to "done" then back to running). */
export function isPending(s: Status | null | undefined): boolean {
  return s == null || s === "live";
}

/** Inverse of ``isPending``. */
export function isDone(s: Status | null | undefined): boolean {
  return !isPending(s);
}
