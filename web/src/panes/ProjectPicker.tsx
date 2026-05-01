import { useProjects } from "@/api/client";
import { useUi } from "@/store/ui";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Badge } from "@/components/ui/badge";
import { FolderOpen, X } from "lucide-react";
import { cn, formatRelativeTime } from "@/lib/utils";

function basename(p: string): string {
  // Strip trailing slashes, then take the last path segment. Falls back to
  // the original string if there's no separator (shouldn't happen for real
  // source paths, but keeps the UI safe).
  const trimmed = p.replace(/\/+$/, "");
  const i = trimmed.lastIndexOf("/");
  return i >= 0 ? trimmed.slice(i + 1) : trimmed || p;
}

export function ProjectPicker({ onClose }: { onClose?: () => void } = {}) {
  const { data, isLoading, error } = useProjects();
  const setSlug = useUi((s) => s.setSlug);
  const selectRootSession = useUi((s) => s.selectRootSession);

  return (
    <div className="mx-auto flex h-full w-full max-w-2xl flex-col">
      <header className="flex items-start justify-between gap-3 px-6 pt-10">
        <div>
          <div className="text-xl font-semibold">unwind</div>
          <div className="mt-1 text-xs text-muted-foreground">
            pick a project to observe. all data lives under{" "}
            <code className="font-mono">~/.claude/projects/</code>.
          </div>
        </div>
        {onClose && (
          <button
            type="button"
            onClick={onClose}
            title="close"
            className="inline-flex h-6 w-6 items-center justify-center rounded text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
          >
            <X className="h-4 w-4" />
          </button>
        )}
      </header>
      <ScrollArea className="flex-1">
        <ul className="divide-y divide-border px-2 py-4">
          {isLoading && (
            <li className="px-4 py-3 text-xs text-muted-foreground">loading…</li>
          )}
          {error && (
            <li className="px-4 py-3 text-xs text-destructive">
              {(error as Error).message}
            </li>
          )}
          {data?.map((p) => (
            <li key={p.slug}>
              <button
                type="button"
                onClick={() => {
                  const url = new URL(window.location.href);
                  url.searchParams.set("project", p.slug);
                  url.searchParams.delete("session");
                  window.history.replaceState({}, "", url.toString());
                  setSlug(p.slug);
                  selectRootSession(null);
                  onClose?.();
                }}
                className={cn(
                  "flex w-full items-center gap-3 rounded-md px-4 py-3 text-left transition-colors",
                  "hover:bg-accent/60",
                )}
              >
                <FolderOpen className="h-4 w-4 shrink-0 text-muted-foreground" />
                <div className="min-w-0 flex-1">
                  <div className="truncate text-sm font-medium text-foreground">
                    {basename(p.source_path)}
                  </div>
                  <div className="truncate text-[10px] text-muted-foreground">
                    {p.source_path}
                  </div>
                </div>
                <Badge variant="outline">
                  {p.session_count} {p.session_count === 1 ? "session" : "sessions"}
                </Badge>
                <span className="w-10 text-right text-[10px] tabular-nums text-muted-foreground">
                  {formatRelativeTime(p.last_activity)}
                </span>
              </button>
            </li>
          ))}
          {data && data.length === 0 && (
            <li className="px-4 py-3 text-xs text-muted-foreground">
              no projects yet — run Claude Code in any folder first.
            </li>
          )}
        </ul>
      </ScrollArea>
    </div>
  );
}
