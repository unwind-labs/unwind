import { describe, it, expect } from "vitest";
import { deriveRows, INVOKE_RESUME_TOOL } from "./derive-rows";
import type { Message } from "@/api/types";

function baseMsg(over: Partial<Message>): Message {
  return {
    uuid: over.uuid ?? "u",
    session_id: "P",
    role: over.role ?? "tool_use",
    timestamp: over.timestamp ?? null,
    text: null,
    tool_name: null,
    tool_input: null,
    tool_use_id: null,
    tool_result_for: null,
    tool_result: null,
    is_error: false,
    model: null,
    raw_type: null,
    origin_session_id: null,
    is_inherited: false,
    spawn_kind: null,
    spawn_session_ids: [],
    spawn_tasks: [],
    ...over,
  };
}

describe("deriveRows — invoke_resume", () => {
  /** Mirrors the verified customer-support JSONL: one ``invoke`` followed by
   *  two ``invoke_resume`` calls, all targeting the same child session. */
  const messages: Message[] = [
    baseMsg({
      uuid: "u1",
      role: "tool_use",
      tool_name: "mcp__plugin_callstack_call__invoke",
      tool_use_id: "tu_invoke",
      timestamp: "2026-05-01T23:44:47Z",
      tool_input: { task: "/authenticate-customer cust_7829" },
      spawn_kind: "call",
      spawn_session_ids: ["151ee68c"],
      spawn_tasks: ["/authenticate-customer cust_7829"],
      spawn_done: [true],
    }),
    baseMsg({
      uuid: "u2",
      role: "tool_result",
      tool_result_for: "tu_invoke",
      timestamp: "2026-05-01T23:45:17Z",
    }),
    baseMsg({
      uuid: "u3",
      role: "tool_use",
      tool_name: INVOKE_RESUME_TOOL,
      tool_use_id: "tu_resume_1",
      timestamp: "2026-05-01T23:45:35Z",
      tool_input: {
        resume_session: "151ee68c",
        user_reply: "000000",
      },
      spawn_kind: "call",
      spawn_session_ids: ["151ee68c"],
      spawn_tasks: ["151ee68c"],
      spawn_done: [true],
    }),
    baseMsg({
      uuid: "u4",
      role: "tool_result",
      tool_result_for: "tu_resume_1",
      timestamp: "2026-05-01T23:45:45Z",
    }),
    baseMsg({
      uuid: "u5",
      role: "tool_use",
      tool_name: INVOKE_RESUME_TOOL,
      tool_use_id: "tu_resume_2",
      timestamp: "2026-05-01T23:46:03Z",
      tool_input: {
        resume_session: "151ee68c",
        user_reply: "yes please",
      },
      spawn_kind: "call",
      spawn_session_ids: ["151ee68c"],
      spawn_tasks: ["151ee68c"],
      spawn_done: [false],
    }),
  ];

  it("emits one spawn row per invocation with distinct handleIds", () => {
    const rows = deriveRows(messages);
    const spawns = rows.filter((r) => r.kind === "spawn");
    expect(spawns).toHaveLength(3);
    expect(spawns.map((r) => r.handleId)).toEqual([
      "spawn-tu_invoke-0",
      "spawn-tu_resume_1-0",
      "spawn-tu_resume_2-0",
    ]);
    // All target the same Claude session id.
    for (const r of spawns) {
      expect(r.kind === "spawn" && r.childId).toBe("151ee68c");
    }
  });

  it("flags resume rows with isResume + carries user_reply", () => {
    const rows = deriveRows(messages);
    const spawns = rows.filter((r) => r.kind === "spawn");
    expect(spawns.map((r) => r.kind === "spawn" && r.isResume)).toEqual([
      false,
      true,
      true,
    ]);
    const replies = spawns.map((r) =>
      r.kind === "spawn" ? r.userReply : null,
    );
    expect(replies).toEqual([undefined, "000000", "yes please"]);
  });

  it("attaches each spawn's parent tool_use timestamp", () => {
    const rows = deriveRows(messages);
    const ts = rows
      .filter((r) => r.kind === "spawn")
      .map((r) => (r.kind === "spawn" ? r.parentToolUseTs : null));
    expect(ts).toEqual([
      "2026-05-01T23:44:47Z",
      "2026-05-01T23:45:35Z",
      "2026-05-01T23:46:03Z",
    ]);
  });

  it("titles resume rows with the user_reply (no decorative prefix)", () => {
    const rows = deriveRows(messages);
    const titles = rows
      .filter((r) => r.kind === "spawn")
      .map((r) => (r.kind === "spawn" ? r.title : ""));
    expect(titles[0]).toContain("/authenticate-customer");
    // The card renders its own continue glyph; titles are plain text.
    expect(titles[1]).toBe("000000");
    expect(titles[2]).toBe("yes please");
  });

  it("propagates per-handle done state from spawn_done", () => {
    const rows = deriveRows(messages);
    const done = rows
      .filter((r) => r.kind === "spawn")
      .map((r) => (r.kind === "spawn" ? r.done : null));
    expect(done).toEqual([true, true, false]);
  });
});

describe("deriveRows — extras handleId uniqueness", () => {
  /** Regression: two different parents each emitted a single extra spawn,
   *  both got handleId ``extra-0-0``. ReactFlow keys nodes by handleId, so
   *  the deeper child silently overwrote the shallower one — its card never
   *  rendered. handleId must include the child sid. */
  it("uses globally-unique handleIds for extras (includes childId)", () => {
    const extras = [
      {
        invoke_id: "",
        started_at: null,
        ended_at: null,
        status: "complete",
        children: ["dbe31b33-aaaa-bbbb-cccc-111111111111"],
        tasks: ["/check-code-expiry"],
      },
    ];
    const rows = deriveRows([], extras);
    const spawns = rows.filter((r) => r.kind === "spawn");
    expect(spawns).toHaveLength(1);
    const handle = spawns[0].kind === "spawn" ? spawns[0].handleId : "";
    // Must NOT be the old positional id; must contain the childId.
    expect(handle).not.toBe("extra-0-0");
    expect(handle).toContain("dbe31b33-aaaa-bbbb-cccc-111111111111");
  });

  it("produces distinct handleIds for the same positional index across calls", () => {
    // Simulate the bug scenario: two separate parents each emit one extra
    // (their first child). Without the fix both produce ``extra-0-0``.
    const parentA = deriveRows([], [
      {
        invoke_id: "",
        started_at: null,
        ended_at: null,
        status: "complete",
        children: ["aaaa1111-aaaa-aaaa-aaaa-aaaaaaaaaaaa"],
        tasks: ["task-a"],
      },
    ]);
    const parentB = deriveRows([], [
      {
        invoke_id: "",
        started_at: null,
        ended_at: null,
        status: "complete",
        children: ["bbbb2222-bbbb-bbbb-bbbb-bbbbbbbbbbbb"],
        tasks: ["task-b"],
      },
    ]);
    const ha = parentA.find((r) => r.kind === "spawn");
    const hb = parentB.find((r) => r.kind === "spawn");
    if (ha?.kind !== "spawn" || hb?.kind !== "spawn") throw new Error("no spawn");
    expect(ha.handleId).not.toBe(hb.handleId);
  });
});
