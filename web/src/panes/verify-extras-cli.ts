/** CLI verification: replay the canvas pipeline against pre-dumped API
 *  responses for a chain of sessions, and report which child nodes the
 *  pipeline produces for each parent.
 *
 *  Used to track down "this session has a child but the canvas doesn't
 *  show it" bugs without spinning up the browser.
 *
 *  Usage:
 *    cd web && npx vite-node src/panes/verify-extras-cli.ts <api-dump.json> <root-sid>
 *
 *  ``api-dump.json`` is a ``{ sessionId: messagesResponse }`` map. */

import { readFileSync } from "node:fs";
import type { MessagesResponse } from "@/api/types";
import { deriveRows } from "./derive-rows";
import { windowsForParent, type SpawnEdgeInfo } from "./instances";

type Dump = Record<string, MessagesResponse>;

function spawnsFor(
  sessionId: string,
  windowStart: string | null,
  windowEnd: string | null,
  isLatest: boolean,
  dump: Dump,
): SpawnEdgeInfo[] {
  const r = dump[sessionId];
  if (!r) return [];
  // Mirror CompactCardNode's window filter.
  const startMs = windowStart ? Date.parse(windowStart) : -Infinity;
  const endMs = windowEnd ? Date.parse(windowEnd) : Infinity;
  const filteredMsgs =
    windowStart == null && windowEnd == null
      ? r.messages
      : r.messages.filter((m) => {
          if (!m.timestamp) return windowStart == null;
          const t = Date.parse(m.timestamp);
          return t >= startMs && t < endMs;
        });
  const extras = isLatest ? r.extra_spawns ?? [] : [];
  const rows = deriveRows(filteredMsgs, extras);
  const out: SpawnEdgeInfo[] = [];
  for (const row of rows) {
    if (row.kind !== "spawn") continue;
    if (!row.childId) continue;
    if (row.handleId === sessionId) continue;
    out.push({
      parent: sessionId,
      child: row.handleId,
      childSessionId: row.childId,
      handleId: row.handleId,
      spawnKind: row.spawnKind,
      done: row.done,
      label: row.title,
      parentToolUseTs: row.parentToolUseTs,
      isResume: row.isResume,
      userReply: row.userReply,
    });
  }
  return out;
}

type Node = {
  nodeId: string;
  sessionId: string;
  windowStart: string | null;
  windowEnd: string | null;
  isLatest: boolean;
  depth: number;
};

function main() {
  const [, , dumpPath, rootSid] = process.argv;
  if (!dumpPath || !rootSid) {
    console.error("usage: verify-extras-cli.ts <dump.json> <root-sid>");
    process.exit(2);
  }
  const dump: Dump = JSON.parse(readFileSync(dumpPath, "utf8"));

  const queue: Node[] = [
    {
      nodeId: rootSid,
      sessionId: rootSid,
      windowStart: null,
      windowEnd: null,
      isLatest: true,
      depth: 0,
    },
  ];
  while (queue.length) {
    const node = queue.shift()!;
    const indent = "  ".repeat(node.depth);
    const winLabel = node.windowEnd == null ? "[latest]" : `[…${node.windowEnd})`;
    console.log(
      `${indent}- ${node.sessionId.slice(0, 8)}  ${winLabel}  nodeId=${node.nodeId.slice(
        0,
        24,
      )}`,
    );
    if (!dump[node.sessionId]) {
      console.log(`${indent}  (no API dump for this session — leaf)`);
      continue;
    }
    const spawns = spawnsFor(
      node.sessionId,
      node.windowStart,
      node.windowEnd,
      node.isLatest,
      dump,
    );
    if (spawns.length === 0) {
      console.log(`${indent}  (no spawns)`);
      continue;
    }
    const windows = windowsForParent(node.nodeId, spawns);
    // Group windows by sessionId, find latest per group.
    const latestBySession = new Map<string, string>();
    for (const w of windows) {
      const existing = latestBySession.get(w.sessionId);
      if (!existing || w.windowEnd === null) latestBySession.set(w.sessionId, w.nodeId);
    }
    for (const w of windows) {
      const isLatest = latestBySession.get(w.sessionId) === w.nodeId;
      queue.push({
        nodeId: w.nodeId,
        sessionId: w.sessionId,
        windowStart: w.windowStart,
        windowEnd: w.windowEnd,
        isLatest,
        depth: node.depth + 1,
      });
    }
  }
}

main();
