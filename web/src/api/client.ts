import { useQuery, useQueryClient } from "@tanstack/react-query";
import type {
  CanvasTreeResponse,
  DefaultProject,
  Message,
  MessagesResponse,
  ProjectSummary,
  SessionRow,
} from "./types";

async function j<T>(url: string): Promise<T> {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url} -> ${r.status}`);
  return (await r.json()) as T;
}

const enc = encodeURIComponent;

export type PickedFolder = {
  cancelled: boolean;
  slug: string | null;
  source_path: string | null;
};

export async function pickFolder(): Promise<PickedFolder> {
  // The POST endpoint requires a short-lived single-use nonce — fetched
  // here, consumed immediately by the POST. Blocks blind-CSRF attempts
  // from triggering the native folder dialog.
  const nr = await fetch("/api/projects/pick-folder-nonce");
  if (!nr.ok) throw new Error(`pick-folder-nonce -> ${nr.status}`);
  const { nonce } = (await nr.json()) as { nonce: string };
  const r = await fetch("/api/projects/pick-folder", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ nonce }),
  });
  if (!r.ok) throw new Error(`pick-folder -> ${r.status}`);
  return (await r.json()) as PickedFolder;
}

export function useDefaultProject() {
  return useQuery({
    queryKey: ["default-project"],
    queryFn: () => j<DefaultProject>("/api/projects/default"),
  });
}

export function useProjects() {
  return useQuery({
    queryKey: ["projects"],
    queryFn: () => j<ProjectSummary[]>("/api/projects"),
  });
}

// The WebSocket is the primary push channel; ws/client.ts invalidates
// each query family on the matching server event. These polling intervals
// are pure safety nets for when the WS is flaky or paused (background tab,
// reconnect window). 30s strikes the balance: 10x fewer HTTP round-trips
// than the prior 3s cadence, while still ensuring the UI converges within
// half a minute of a missed event.
const POLL_SAFETY_NET_MS = 30_000;

export function useSessions(
  slug: string | null | undefined,
  includeForks: boolean = false,
) {
  return useQuery({
    enabled: !!slug,
    queryKey: ["sessions", slug, includeForks],
    queryFn: () =>
      j<SessionRow[]>(
        `/api/projects/${enc(slug!)}/sessions?include_forks=${includeForks}`,
      ),
    refetchInterval: POLL_SAFETY_NET_MS,
  });
}

/** The canvas window-tree for a root session. Computed server-side
 *  from session JSONLs + callstack reports in one deterministic pass —
 *  the frontend just renders the result. */
export function useCanvasTree(
  slug: string | null | undefined,
  rootSessionId: string | null | undefined,
) {
  return useQuery({
    enabled: !!slug && !!rootSessionId,
    queryKey: ["canvas-tree", slug, rootSessionId],
    queryFn: () =>
      j<CanvasTreeResponse>(
        `/api/projects/${enc(slug!)}/sessions/${enc(rootSessionId!)}/canvas`,
      ),
    refetchInterval: POLL_SAFETY_NET_MS,
  });
}

export function useMessages(
  slug: string | null | undefined,
  sessionId: string | null | undefined,
  includeMeta: boolean = false,
) {
  const qc = useQueryClient();
  return useQuery({
    enabled: !!slug && !!sessionId,
    queryKey: ["messages", slug, sessionId, includeMeta],
    queryFn: async () => {
      const key = ["messages", slug, sessionId, includeMeta];
      const prev = qc.getQueryData<MessagesResponse>(key);
      const params = new URLSearchParams({ include_meta: String(includeMeta) });
      // Delta fetch: when we already hold a snapshot, ask the server only
      // for messages that landed after the last record we saw. Server still
      // returns the full file_offset / last_uuid so we can keep tailing.
      if (prev?.last_uuid) params.set("since_uuid", prev.last_uuid);
      const fresh = await j<MessagesResponse>(
        `/api/projects/${enc(slug!)}/sessions/${enc(sessionId!)}/messages?${params}`,
      );
      if (!prev?.last_uuid) return fresh;
      return mergeMessagesDelta(prev, fresh);
    },
    refetchInterval: POLL_SAFETY_NET_MS,
  });
}

/** Merge a delta response into the cached snapshot. Dedup by uuid in case
 *  the server falls back to a full payload (e.g. unknown since_uuid after
 *  file rotation) or the WS already patched in some of the same rows. */
function mergeMessagesDelta(
  prev: MessagesResponse,
  delta: MessagesResponse,
): MessagesResponse {
  const seen = new Set(prev.messages.map((m) => m.uuid));
  const merged: Message[] = [...prev.messages];
  for (const m of delta.messages) {
    if (!seen.has(m.uuid)) {
      merged.push(m);
      seen.add(m.uuid);
    }
  }
  return {
    ...delta,
    messages: merged,
  };
}
