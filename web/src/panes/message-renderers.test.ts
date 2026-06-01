import { describe, it, expect } from "vitest";
import { describeTool, describeSystem, lineDiff } from "./message-renderers";
import type { Message } from "@/api/types";

function msg(over: Partial<Message>): Message {
  return {
    uuid: "u",
    session_id: "P",
    role: over.role ?? "tool_use",
    timestamp: null,
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
    ...over,
  };
}

describe("describeTool", () => {
  it("Read shows basename, line count and byte size from the result", () => {
    const tu = msg({ tool_name: "Read", tool_input: { file_path: "/a/b/TracePane.tsx" } });
    // Three lines, last has no trailing newline → 3 lines.
    const v = describeTool(tu, "line1\nline2\nline3");
    expect(v.label).toBe("Read");
    expect(v.summary).toContain("TracePane.tsx");
    expect(v.summary).toContain("3 lines");
    expect(v.summary).toMatch(/B|KB/);
  });

  it("Read surfaces an offset and a pending state before the result lands", () => {
    const tu = msg({ tool_name: "Read", tool_input: { file_path: "/x/LOG.md", offset: 30 } });
    const v = describeTool(tu, null);
    expect(v.summary).toContain("from line 30");
    expect(v.summary).toContain("reading…");
  });

  it("Edit reports a +added/−removed line stat from the strings, not the result", () => {
    const tu = msg({
      tool_name: "Edit",
      tool_input: { file_path: "/p/x.ts", old_string: "a\nb", new_string: "a\nb\nc\nd" },
    });
    const v = describeTool(tu, "ok");
    expect(v.label).toBe("Edit");
    expect(v.summary).toBe("x.ts · +4 −2");
  });

  it("Bash prefers the description over the raw command", () => {
    const tu = msg({
      tool_name: "Bash",
      tool_input: { command: "ls -la | head", description: "List files" },
    });
    expect(describeTool(tu, "").summary).toBe("List files");
  });

  it("an MCP tool de-prefixes the name and keeps the server as the detail", () => {
    const tu = msg({ tool_name: "mcp__trello__get_card", tool_input: {} });
    const v = describeTool(tu, null);
    expect(v.label).toBe("get_card");
    expect(v.summary).toBe("via trello");
  });
});

describe("describeSystem", () => {
  it("skill_listing counts the dash-prefixed entries", () => {
    const m = msg({
      role: "system",
      raw_type: "skill_listing",
      text: "- alpha: do a\n- beta: do b\n- gamma: do c",
    });
    const v = describeSystem(m);
    expect(v.label).toBe("Skills available");
    expect(v.detail).toBe("3");
  });

  it("hook_success extracts the hook name from the [name] prefix", () => {
    const m = msg({
      role: "system",
      raw_type: "hook_success",
      text: "[SessionStart:startup] done",
    });
    const v = describeSystem(m);
    expect(v.detail).toBe("SessionStart:startup");
    expect(v.body).toBe("done");
  });

  it("deferred_tools_delta summarises the added/removed counts", () => {
    const m = msg({
      role: "system",
      raw_type: "deferred_tools_delta",
      text: "added: CronCreate, CronDelete, CronList\nremoved: OldTool",
    });
    expect(describeSystem(m).detail).toBe("+3 −1");
  });

  it("skill_listing is flagged for markdown rendering", () => {
    const m = msg({ role: "system", raw_type: "skill_listing", text: "- a: x" });
    expect(describeSystem(m).markdown).toBe(true);
  });
});

describe("lineDiff", () => {
  it("keeps common lines as context and marks only real changes", () => {
    const rows = lineDiff("a\nb\nc", "a\nB\nc");
    expect(rows).toEqual([
      { type: "ctx", text: "a" },
      { type: "del", text: "b" },
      { type: "add", text: "B" },
      { type: "ctx", text: "c" },
    ]);
  });

  it("treats pure additions as adds with no spurious deletions", () => {
    const rows = lineDiff("a\nb", "a\nb\nc\nd");
    expect(rows.filter((r) => r.type === "del")).toHaveLength(0);
    expect(rows.filter((r) => r.type === "add").map((r) => r.text)).toEqual(["c", "d"]);
  });
});
