/** CLI verification: run deriveRows + instancesForParent against the real
 *  customer-support JSONL and print the resulting per-instance windows.
 *
 *  Usage:
 *    cd web && npx vite-node src/panes/verify-cli.ts <path-to.jsonl> <child-sid>
 *
 *  Stands in for the backend annotation step (which needs callstack reports
 *  on disk) by reading raw JSONL records directly and synthesizing the
 *  ``spawn_session_ids`` field from each invoke/invoke_resume tool_use's
 *  ``input``. That's the only piece the backend would normally fill — every
 *  other Message field comes straight from the JSONL. */

import { readFileSync } from "node:fs";
import type { Message } from "@/api/types";
import { deriveRows, INVOKE_RESUME_TOOL } from "./derive-rows";
import { windowsForParent, type SpawnEdgeInfo } from "./instances";

const CALLSTACK_TOOLS = new Set([
  "mcp__plugin_callstack_call__invoke",
  "mcp__plugin_callstack_call__invoke_parallel",
  INVOKE_RESUME_TOOL,
]);

function loadMessages(path: string, childSid: string): Message[] {
  const out: Message[] = [];
  const text = readFileSync(path, "utf8");
  for (const line of text.split("\n")) {
    if (!line.trim()) continue;
    let rec: Record<string, unknown>;
    try {
      rec = JSON.parse(line);
    } catch {
      continue;
    }
    const msg = (rec.message as Record<string, unknown>) ?? {};
    const content = msg.content as unknown;
    if (!Array.isArray(content)) continue;
    for (const c of content) {
      if (!c || typeof c !== "object") continue;
      const block = c as Record<string, unknown>;
      const t = block.type;
      const ts = (rec.timestamp as string) ?? null;
      if (t === "tool_use") {
        const tool = (block.name as string) ?? "";
        const id = (block.id as string) ?? "";
        const input = block.input ?? null;
        // Mimic backend annotation: any callstack tool_use that targets our
        // tracked child sid (via input.task / input.resume_session) gets
        // spawn_kind=call + spawn_session_ids=[childSid].
        let spawn_kind: "call" | null = null;
        let spawn_session_ids: string[] = [];
        if (CALLSTACK_TOOLS.has(tool)) {
          const inp = input as Record<string, unknown> | null;
          const isResume = tool === INVOKE_RESUME_TOOL;
          if (isResume && inp?.resume_session === childSid) {
            spawn_kind = "call";
            spawn_session_ids = [childSid];
          } else if (!isResume && inp?.task != null) {
            // Original invoke — pretend the backend resolved it.
            spawn_kind = "call";
            spawn_session_ids = [childSid];
          }
        }
        out.push({
          uuid: (rec.uuid as string) ?? "",
          session_id: (rec.sessionId as string) ?? "",
          role: "tool_use",
          timestamp: ts,
          text: null,
          tool_name: tool,
          tool_input: input,
          tool_use_id: id,
          tool_result_for: null,
          tool_result: null,
          is_error: false,
          model: null,
          raw_type: "tool_use",
          origin_session_id: null,
          is_inherited: false,
          spawn_kind,
          spawn_session_ids,
          spawn_tasks: spawn_session_ids.map(() => ""),
        });
      } else if (t === "tool_result") {
        out.push({
          uuid: (rec.uuid as string) ?? "",
          session_id: (rec.sessionId as string) ?? "",
          role: "tool_result",
          timestamp: ts,
          text: null,
          tool_name: null,
          tool_input: null,
          tool_use_id: null,
          tool_result_for: (block.tool_use_id as string) ?? null,
          tool_result: block.content ?? null,
          is_error: false,
          model: null,
          raw_type: "tool_result",
          origin_session_id: null,
          is_inherited: false,
          spawn_kind: null,
          spawn_session_ids: [],
          spawn_tasks: [],
        });
      }
    }
  }
  return out;
}

function main() {
  const [, , jsonlPath, childSid] = process.argv;
  if (!jsonlPath || !childSid) {
    console.error("usage: verify-cli.ts <jsonl> <child-sid>");
    process.exit(2);
  }
  const messages = loadMessages(jsonlPath, childSid);
  const rows = deriveRows(messages);
  const spawns = rows.filter((r) => r.kind === "spawn");
  console.log(`spawn rows in parent JSONL: ${spawns.length}`);

  // Now run the canvas's instance derivation as if these were the parent's
  // resolved spawns. ``parent`` field is just a label here.
  const edges: SpawnEdgeInfo[] = spawns.flatMap((r) =>
    r.kind === "spawn" && r.childId
      ? [
          {
            parent: "P",
            child: r.handleId,
            childSessionId: r.childId,
            handleId: r.handleId,
            spawnKind: r.spawnKind,
            done: r.done,
            label: r.title,
            parentToolUseTs: r.parentToolUseTs,
            isResume: r.isResume,
            userReply: r.userReply,
          },
        ]
      : [],
  );

  const windows = windowsForParent("P", edges);
  const sorted = windows.slice().sort((a, b) =>
    (a.windowStart ?? "").localeCompare(b.windowStart ?? ""),
  );
  console.log(`windows derived: ${windows.length}`);
  sorted.forEach((win, i) => {
    const tag = win.isResume ? "↻ resume" : "  invoke";
    const end = win.windowEnd ?? "(open)";
    const latest = win.windowEnd === null ? " [latest]" : "";
    console.log(
      `  [${i}/${windows.length - 1}] ${tag}  nodeId=${win.nodeId.slice(
        0,
        20,
      )}…  sid=${win.sessionId.slice(0, 8)}  [${win.windowStart}, ${end})${latest}`,
    );
  });

  // Sanity assertions matching the verified JSONL shape.
  const errors: string[] = [];
  if (spawns.length < 3) errors.push(`expected ≥3 spawn rows, got ${spawns.length}`);
  if (windows.length !== spawns.filter((r) => r.kind === "spawn" && r.childId).length)
    errors.push(`window count mismatch`);
  const sids = new Set(windows.map((w) => w.sessionId));
  if (sids.size !== 1) errors.push(`expected 1 distinct child sid, got ${sids.size}`);
  const handles = new Set(windows.map((w) => w.nodeId));
  if (handles.size !== windows.length)
    errors.push(`duplicate nodeIds (handle collision)`);
  if (sorted[sorted.length - 1].windowEnd !== null)
    errors.push(`latest window's windowEnd should be null`);
  for (let i = 0; i < sorted.length - 1; i++) {
    if (sorted[i].windowEnd !== sorted[i + 1].windowStart)
      errors.push(`window boundary mismatch at ${i}`);
  }

  if (errors.length) {
    console.error("\nFAIL:");
    for (const e of errors) console.error("  -", e);
    process.exit(1);
  }
  console.log("\nOK — frontend pipeline produces 3 windows with correct boundaries.");
}

main();
