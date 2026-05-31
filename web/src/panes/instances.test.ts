import { describe, it, expect } from "vitest";
import { filterMessagesByWindow, groupMessages } from "./instances";
import type { Message } from "@/api/types";

function msg(uuid: string, timestamp: string | null): Message {
  return {
    uuid,
    session_id: "S",
    role: "user",
    timestamp,
    text: null,
    tool_name: null,
    tool_input: null,
    tool_use_id: null,
    tool_result_for: null,
    tool_result: null,
    is_error: false,
    model: null,
    raw_type: null,
    spawn_kind: null,
    spawn_session_ids: [],
    spawn_tasks: [],
  };
}

function toolUse(uuid: string, ts: string, toolUseId: string): Message {
  return { ...msg(uuid, ts), role: "tool_use", tool_use_id: toolUseId };
}

function toolResult(uuid: string, ts: string, toolUseId: string): Message {
  return { ...msg(uuid, ts), role: "tool_result", tool_result_for: toolUseId };
}

/** Pins the half-open ``[start, end)`` partition invariant.
 *
 *  Why this matters: ``filterMessagesByWindow`` slices a session's message
 *  stream into per-instance windows (one card on the canvas, one slice of
 *  the trace pane). Adjacent windows MUST tile without overlap and without
 *  gaps — every timestamped message lands in exactly one window. The
 *  natural convention is half-open: ``windowEnd`` of instance N equals
 *  ``windowStart`` of instance N+1, and the boundary message belongs to
 *  N+1 (the "starts at" interpretation, matching how ``windowEnd`` is set
 *  to the next ``invoke_resume`` tool_use timestamp).
 *
 *  Consumers that need to relate a tool_use in window N to its tool_result
 *  in window N+1 (cross-window pairing) must consult the FULL unwindowed
 *  stream — see ``deriveRows``' third-arg ``allMessages`` parameter and the
 *  ``spawn_status`` field on tool_use messages. Bending the filter to be
 *  inclusive would silently double-count in activity buckets and trace
 *  rendering. */
describe("filterMessagesByWindow — half-open boundary invariant", () => {
  const T0 = "2026-05-01T00:00:00.000Z";
  const T1 = "2026-05-01T00:00:01.000Z";
  const T2 = "2026-05-01T00:00:02.000Z";
  const T3 = "2026-05-01T00:00:03.000Z";

  const messages: Message[] = [msg("m0", T0), msg("m1", T1), msg("m2", T2), msg("m3", T3)];

  it("includes a message whose timestamp equals windowStart", () => {
    const got = filterMessagesByWindow(messages, T1, T3).map((m) => m.uuid);
    expect(got).toContain("m1");
  });

  it("excludes a message whose timestamp equals windowEnd (exclusive end)", () => {
    const got = filterMessagesByWindow(messages, T1, T2).map((m) => m.uuid);
    expect(got).not.toContain("m2");
    expect(got).toEqual(["m1"]);
  });

  it("places a boundary message in the LATER window when two adjacent windows share an edge", () => {
    // Two adjacent windows: [T0, T2) and [T2, T3). The message at T2 must
    // appear in window B, not window A — that's the convention that lines
    // up with how ``windowEnd`` is set (= next resume's tool_use ts, which
    // is itself a message that belongs to the resumed instance).
    const windowA = filterMessagesByWindow(messages, T0, T2).map((m) => m.uuid);
    const windowB = filterMessagesByWindow(messages, T2, T3).map((m) => m.uuid);
    expect(windowA).toEqual(["m0", "m1"]);
    expect(windowB).toEqual(["m2"]);
  });

  it("tiles adjacent windows without overlap or gap across the boundary", () => {
    // Same scenario as above, asserted as a set-level invariant: the union
    // of any two adjacent half-open windows equals the messages in their
    // outer range, and their intersection is empty. If anyone "fixes" the
    // filter to inclusive end this test catches the duplication.
    const windowA = new Set(filterMessagesByWindow(messages, T0, T2).map((m) => m.uuid));
    const windowB = new Set(filterMessagesByWindow(messages, T2, T3).map((m) => m.uuid));
    const union = new Set([...windowA, ...windowB]);
    const intersection = [...windowA].filter((u) => windowB.has(u));
    expect(intersection).toEqual([]);
    expect(union).toEqual(new Set(["m0", "m1", "m2"]));
  });

  it("open-ended windows (null end) still include all later messages", () => {
    const got = filterMessagesByWindow(messages, T1, null).map((m) => m.uuid);
    expect(got).toEqual(["m1", "m2", "m3"]);
  });

  it("open-start windows (null start) include the first message", () => {
    const got = filterMessagesByWindow(messages, null, T2).map((m) => m.uuid);
    expect(got).toEqual(["m0", "m1"]);
  });
});

/** Pins cross-window tool_result borrowing.
 *
 *  Why this matters: the trace pane filters messages with
 *  ``filterMessagesByWindow`` which uses a half-open ``[start, end)`` boundary
 *  (see the test block above). When a child's ``tool_result`` timestamp
 *  collides with the next ``invoke_resume``'s ``tool_use`` timestamp — they
 *  can land on the same millisecond — the result is excluded from the window
 *  where its originating ``tool_use`` lives. Without borrowing, the user
 *  expanding the SpawnCard / ToolCard in that window sees no result body.
 *
 *  Contract: ``groupMessages`` accepts an optional ``allMessages`` parameter
 *  (the full unwindowed stream) and, for any ``tool_use`` in the windowed
 *  slice whose matching ``tool_result`` is missing, borrows the result by
 *  ``tool_use_id`` from ``allMessages``. The boundary ``tool_result`` is NOT
 *  duplicated into the next window's groups — it still belongs there and is
 *  rendered normally when that window is shown. */
describe("groupMessages — boundary tool_result borrowing", () => {
  const T0 = "2026-05-01T00:00:00.000Z";
  const T1 = "2026-05-01T00:00:01.000Z";
  const T2 = "2026-05-01T00:00:02.000Z";

  it("borrows a tool_result from the full stream when its tool_use is in the window but the result equals windowEnd", () => {
    // Scenario: a child's tool_result lands at exactly window_end (T2),
    // which is the same instant as the next invoke_resume's tool_use.
    // The tool_use that spawned it lives at T1, inside the window [T1, T2).
    const tu = toolUse("u1", T1, "tu1");
    const tr = toolResult("r1", T2, "tu1");
    const all = [tu, tr];
    const windowed = filterMessagesByWindow(all, T1, T2);
    // Sanity: the half-open filter excludes the boundary tool_result.
    expect(windowed.map((m) => m.uuid)).toEqual(["u1"]);

    const groups = groupMessages(windowed, all);
    expect(groups).toHaveLength(1);
    expect(groups[0].kind).toBe("tool");
    if (groups[0].kind === "tool") {
      expect(groups[0].toolResult?.uuid).toBe("r1");
    }
  });

  it("does NOT borrow when allMessages is omitted (preserves legacy single-arg behavior)", () => {
    const tu = toolUse("u1", T1, "tu1");
    const tr = toolResult("r1", T2, "tu1");
    const windowed = filterMessagesByWindow([tu, tr], T1, T2);
    const groups = groupMessages(windowed);
    expect(groups).toHaveLength(1);
    if (groups[0].kind === "tool") {
      expect(groups[0].toolResult).toBeUndefined();
    }
  });

  it("prefers an in-window tool_result over a borrowed one", () => {
    // If the result actually IS in the window, no borrowing should happen.
    const tu = toolUse("u1", T0, "tu1");
    const trIn = toolResult("r1", T1, "tu1");
    const all = [tu, trIn];
    const windowed = filterMessagesByWindow(all, T0, T2);
    const groups = groupMessages(windowed, all);
    expect(groups).toHaveLength(1);
    if (groups[0].kind === "tool") {
      expect(groups[0].toolResult?.uuid).toBe("r1");
    }
  });

  it("leaves orphan tool_results inside the window as their own message group (unchanged behavior)", () => {
    // tool_result whose tool_use is not in the windowed slice — current
    // behavior renders it as a standalone msg group. Borrowing must not
    // change that.
    const tr = toolResult("r1", T1, "tu-missing");
    const groups = groupMessages([tr], [tr]);
    expect(groups).toHaveLength(1);
    expect(groups[0].kind).toBe("msg");
  });
});
