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

/** Polls every 3s so the left pane's status dot stays in lockstep
 *  with the canvas main-node status (which polls at the same rate via
 *  ``useCanvasTree``). Without this matching cadence the left pane
 *  could lag the canvas by up to 30 seconds — visually jarring when
 *  the user just resumed a session. */
export function useSessions(
  slug: string | null | undefined,
  includeForks: boolean = false,
) {
  return useQuery({
    enabled: !!slug,
    queryKey: ["sessions", slug, includeForks],
    queryFn: () =>
      j<SessionRow[]>(
        `/api/projects/${slug}/sessions?include_forks=${includeForks}`,
      ),
    refetchInterval: 3_000,
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
        `/api/projects/${slug}/sessions/${rootSessionId}/canvas`,
      ),
    // Same cadence as messages — the WS will normally invalidate
    // sooner; this is a safety net.
    refetchInterval: 3_000,
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
        `/api/projects/${slug}/sessions/${sessionId}/messages?include_meta=${includeMeta}`,
      ),
    // Refresh aggressively while a session is in flight so the canvas picks
    // up new spawns and child sessions as callstack updates report.yaml.
    // The WebSocket also invalidates on tree_changed, but this is a safety
    // net for environments where the WS is flaky.
    refetchInterval: 3_000,
  });
}
