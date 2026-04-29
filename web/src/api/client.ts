import { useQuery } from "@tanstack/react-query";
import type {
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

/**
 * With the WebSocket feeding incremental updates we rely on occasional polling
 * only as a safety net (e.g. if the WS drops and hasn't reconnected yet).
 */
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
    refetchInterval: 30_000,
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
