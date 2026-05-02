import { describe, it, expect } from "vitest";
import {
  filterMessagesByWindow,
  windowsForParent,
  labelForResume,
  type SpawnEdgeInfo,
} from "./instances";
import type { Message } from "@/api/types";

const baseSpawn: Omit<SpawnEdgeInfo, "handleId" | "parentToolUseTs" | "isResume"> = {
  parent: "P",
  child: "C",
  childSessionId: "S",
  spawnKind: "call",
  done: true,
  label: "task",
  userReply: undefined,
};

function spawn(
  handleId: string,
  parentToolUseTs: string | null,
  opts: Partial<SpawnEdgeInfo> = {},
): SpawnEdgeInfo {
  return {
    ...baseSpawn,
    handleId,
    parentToolUseTs,
    isResume: false,
    ...opts,
  };
}

describe("windowsForParent", () => {
  // Sort windows by windowStart ascending — the natural chronological order.
  const byStart = (a: { windowStart: string | null }, b: { windowStart: string | null }) =>
    (a.windowStart ?? "").localeCompare(b.windowStart ?? "");

  it("returns one open-ended window per spawn for distinct sessions", () => {
    const out = windowsForParent("P", [
      spawn("h1", "2026-05-01T00:00:00Z", { childSessionId: "A" }),
      spawn("h2", "2026-05-01T00:01:00Z", { childSessionId: "B" }),
    ]);
    expect(out).toHaveLength(2);
    expect(out.map((w) => w.nodeId).sort()).toEqual(["h1", "h2"]);
    for (const w of out) {
      expect(w.windowEnd).toBeNull();
    }
  });

  it("partitions multiple invocations of one session into chronological windows", () => {
    // Mirrors the verified customer-support sequence: 1 invoke + 2 invoke_resume.
    const out = windowsForParent("P", [
      spawn("h-invoke", "2026-05-01T23:44:47Z"),
      spawn("h-resume1", "2026-05-01T23:45:35Z", { isResume: true }),
      spawn("h-resume2", "2026-05-01T23:46:03Z", { isResume: true }),
    ]);
    expect(out).toHaveLength(3);
    const sorted = out.slice().sort(byStart);
    expect(sorted[0]).toMatchObject({
      nodeId: "h-invoke",
      windowStart: "2026-05-01T23:44:47Z",
      windowEnd: "2026-05-01T23:45:35Z",
      isResume: false,
    });
    expect(sorted[1]).toMatchObject({
      nodeId: "h-resume1",
      windowStart: "2026-05-01T23:45:35Z",
      windowEnd: "2026-05-01T23:46:03Z",
      isResume: true,
    });
    expect(sorted[2]).toMatchObject({
      nodeId: "h-resume2",
      windowStart: "2026-05-01T23:46:03Z",
      windowEnd: null,
      isResume: true,
    });
  });

  it("orders windows by timestamp regardless of input order", () => {
    const out = windowsForParent("P", [
      spawn("late", "2026-05-01T23:46:03Z"),
      spawn("early", "2026-05-01T23:44:47Z"),
      spawn("mid", "2026-05-01T23:45:35Z"),
    ]);
    const byNode = Object.fromEntries(out.map((w) => [w.nodeId, w]));
    expect(byNode.early.windowStart).toBe("2026-05-01T23:44:47Z");
    expect(byNode.early.windowEnd).toBe("2026-05-01T23:45:35Z");
    expect(byNode.mid.windowEnd).toBe("2026-05-01T23:46:03Z");
    expect(byNode.late.windowEnd).toBeNull();
  });

  it("ignores spawns with empty childSessionId", () => {
    const out = windowsForParent("P", [
      spawn("h1", "2026-05-01T00:00:00Z", { childSessionId: "" }),
      spawn("h2", "2026-05-01T00:01:00Z", { childSessionId: "S" }),
    ]);
    expect(out).toHaveLength(1);
    expect(out[0].nodeId).toBe("h2");
  });

  it("flags every non-first window as a resume even if isResume isn't set on the spawn", () => {
    const out = windowsForParent("P", [
      spawn("h1", "2026-05-01T00:00:00Z"),
      spawn("h2", "2026-05-01T00:01:00Z"),
    ]);
    const sorted = out.slice().sort(byStart);
    expect(sorted[0].isResume).toBe(false);
    expect(sorted[1].isResume).toBe(true);
  });
});

describe("filterMessagesByWindow", () => {
  function msg(uuid: string, ts: string | null): Message {
    return {
      uuid,
      session_id: "S",
      role: "tool_use",
      timestamp: ts,
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
    };
  }

  it("keeps everything when both ends are null", () => {
    const ms = [msg("a", "2026-05-01T00:00:00Z"), msg("b", null)];
    expect(filterMessagesByWindow(ms, null, null)).toHaveLength(2);
  });

  it("filters by [start, end) — start inclusive, end exclusive", () => {
    const ms = [
      msg("a", "2026-05-01T23:44:00Z"),
      msg("b", "2026-05-01T23:45:00Z"), // exactly start
      msg("c", "2026-05-01T23:45:30Z"),
      msg("d", "2026-05-01T23:46:00Z"), // exactly end — excluded
      msg("e", "2026-05-01T23:46:30Z"),
    ];
    const out = filterMessagesByWindow(
      ms,
      "2026-05-01T23:45:00Z",
      "2026-05-01T23:46:00Z",
    );
    expect(out.map((m) => m.uuid)).toEqual(["b", "c"]);
  });

  it("open-ended end keeps everything from start onward", () => {
    const ms = [
      msg("a", "2026-05-01T23:44:00Z"),
      msg("b", "2026-05-01T23:45:00Z"),
      msg("c", "2026-05-01T23:45:30Z"),
    ];
    const out = filterMessagesByWindow(ms, "2026-05-01T23:45:00Z", null);
    expect(out.map((m) => m.uuid)).toEqual(["b", "c"]);
  });

  it("drops messages without timestamps when a start is set", () => {
    const ms = [msg("a", null), msg("b", "2026-05-01T23:45:30Z")];
    const out = filterMessagesByWindow(ms, "2026-05-01T23:45:00Z", null);
    expect(out.map((m) => m.uuid)).toEqual(["b"]);
  });

  it("keeps messages without timestamps when start is null", () => {
    const ms = [msg("a", null), msg("b", "2026-05-01T23:45:30Z")];
    const out = filterMessagesByWindow(ms, null, "2026-05-01T23:46:00Z");
    expect(out.map((m) => m.uuid)).toEqual(["a", "b"]);
  });
});

describe("labelForResume", () => {
  it("falls back to '(resumed)' on empty input", () => {
    expect(labelForResume(null)).toBe("(resumed)");
    expect(labelForResume(undefined)).toBe("(resumed)");
    expect(labelForResume("   ")).toBe("(resumed)");
  });

  it("returns the reply as plain text — no decorative prefix", () => {
    // The card renders its own continue glyph; the label is just the text.
    expect(labelForResume("000000")).toBe("000000");
  });

  it("truncates long replies with an ellipsis", () => {
    const long = "x".repeat(100);
    const out = labelForResume(long);
    expect(out.endsWith("…")).toBe(true);
    expect(out.length).toBeLessThanOrEqual(40 + 1);
  });
});
