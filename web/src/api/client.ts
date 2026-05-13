import { useQuery } from "@tanstack/react-query";
import type {
  CanvasTreeResponse,
  DefaultProject,
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
  const r = await fetch("/api/projects/pick-folder", { method: "POST" });
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
  return useQuery({
    enabled: !!slug && !!sessionId,
    queryKey: ["messages", slug, sessionId, includeMeta],
    queryFn: () =>
      j<MessagesResponse>(
        `/api/projects/${enc(slug!)}/sessions/${enc(sessionId!)}/messages?include_meta=${includeMeta}`,
      ),
    refetchInterval: POLL_SAFETY_NET_MS,
  });
}
