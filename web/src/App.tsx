import { useCallback, useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { pickFolder, useDefaultProject, useProjects } from "@/api/client";
import { useUi } from "@/store/ui";
import { useLiveEvents } from "@/ws/client";
import {
  ResizableHandle,
  ResizablePanel,
  ResizablePanelGroup,
} from "@/components/ui/resizable";
import { SessionListPane } from "@/panes/SessionListPane";
import { CanvasPane } from "@/panes/CanvasPane";
import { ProjectPicker } from "@/panes/ProjectPicker";
import { FolderSearch, FolderTree } from "lucide-react";

export function App() {
  const slug = useUi((s) => s.slug);
  const setSlug = useUi((s) => s.setSlug);
  const [showBrowser, setShowBrowser] = useState(false);

  const { data: defaultProject } = useDefaultProject();
  useLiveEvents(slug);

  const selectRootSession = useUi((s) => s.selectRootSession);
  const rootId = useUi((s) => s.rootSessionId);

  // Auto-select a project ONLY on first mount (URL param wins, then the
  // server's default-project hint). Without the guard, clicking "switch
  // project" would set slug=null and this effect would immediately set it
  // back to the cached default — making the picker un-openable.
  const autoSelected = useRef(false);
  useEffect(() => {
    if (autoSelected.current) return;
    if (slug) {
      autoSelected.current = true;
      return;
    }
    const params = new URLSearchParams(window.location.search);
    const queryProject = params.get("project");
    if (queryProject) {
      autoSelected.current = true;
      setSlug(queryProject);
      const querySession = params.get("session");
      if (querySession) selectRootSession(querySession);
      return;
    }
    if (defaultProject?.slug) {
      autoSelected.current = true;
      setSlug(defaultProject.slug);
    }
  }, [slug, defaultProject, setSlug, selectRootSession]);

  // Reflect selection into the URL so refresh and bookmarks work.
  useEffect(() => {
    if (!slug) return;
    const url = new URL(window.location.href);
    url.searchParams.set("project", slug);
    if (rootId) url.searchParams.set("session", rootId);
    else url.searchParams.delete("session");
    window.history.replaceState({}, "", url.toString());
  }, [slug, rootId]);

  // Global ← / → to switch focus between panes; cmd/ctrl+O opens the folder
  // picker. Per-pane up/down handlers live in the panes themselves.
  const rotateFocus = useUi((s) => s.rotateFocus);
  const openPicker = useOpenFolderPicker();
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      const typing =
        target &&
        (target.tagName === "INPUT" ||
          target.tagName === "TEXTAREA" ||
          target.isContentEditable);
      // cmd/ctrl+O fires regardless of focus — matches the OS-wide convention
      // for "Open…". We still skip it if a modifier-stacked variant (shift,
      // alt) is active so we don't steal browser shortcuts like ⌘⇧O.
      if (
        (e.metaKey || e.ctrlKey) &&
        !e.shiftKey &&
        !e.altKey &&
        (e.key === "o" || e.key === "O")
      ) {
        e.preventDefault();
        void openPicker();
        return;
      }
      if (typing) return;
      if (e.key === "ArrowLeft") {
        e.preventDefault();
        rotateFocus(-1);
      } else if (e.key === "ArrowRight") {
        e.preventDefault();
        rotateFocus(1);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [rotateFocus, openPicker]);

  const focusedPane = useUi((s) => s.focusedPane);
  const focusPane = useUi((s) => s.focusPane);

  return (
    <div className="flex h-screen flex-col bg-background text-foreground">
      <TopBar
        defaultSourcePath={defaultProject?.source_path ?? null}
        slug={slug}
        onBrowse={() => setShowBrowser(true)}
      />
      <div className="relative flex-1 overflow-hidden">
        {slug ? (
          <ResizablePanelGroup direction="horizontal">
            <ResizablePanel defaultSize={26} minSize={18}>
              <PaneFrame paneKey="sessions" focusedPane={focusedPane} onFocus={focusPane}>
                <SessionListPane />
              </PaneFrame>
            </ResizablePanel>
            <ResizableHandle />
            <ResizablePanel defaultSize={74} minSize={40}>
              <PaneFrame paneKey="thread" focusedPane={focusedPane} onFocus={focusPane}>
                <CanvasPane />
              </PaneFrame>
            </ResizablePanel>
          </ResizablePanelGroup>
        ) : (
          <ProjectPicker />
        )}
        {showBrowser && slug && (
          <div
            className="absolute inset-0 z-20 bg-background/95 backdrop-blur-sm"
            onMouseDown={(e) => {
              if (e.target === e.currentTarget) setShowBrowser(false);
            }}
          >
            <ProjectPicker onClose={() => setShowBrowser(false)} />
          </div>
        )}
      </div>
    </div>
  );
}

function PaneFrame({
  paneKey,
  focusedPane,
  onFocus,
  children,
}: {
  paneKey: import("@/store/ui").PaneKey;
  focusedPane: import("@/store/ui").PaneKey;
  onFocus: (k: import("@/store/ui").PaneKey) => void;
  children: React.ReactNode;
}) {
  const focused = focusedPane === paneKey;
  return (
    <div
      onMouseDown={() => onFocus(paneKey)}
      className={
        "relative h-full transition-shadow " +
        (focused ? "ring-1 ring-inset ring-primary/40" : "")
      }
    >
      {children}
    </div>
  );
}

function useOpenFolderPicker() {
  const setSlug = useUi((s) => s.setSlug);
  const selectRootSession = useUi((s) => s.selectRootSession);
  const queryClient = useQueryClient();
  return useCallback(async () => {
    const result = await pickFolder();
    if (result.cancelled || !result.slug) return;
    // Reflect into URL so refresh keeps the new project.
    const url = new URL(window.location.href);
    url.searchParams.set("project", result.slug);
    url.searchParams.delete("session");
    window.history.replaceState({}, "", url.toString());
    setSlug(result.slug);
    selectRootSession(null);
    // Refetch the project list so the title bar can resolve the new path.
    queryClient.invalidateQueries({ queryKey: ["projects"] });
  }, [setSlug, selectRootSession, queryClient]);
}

function TopBar({
  defaultSourcePath,
  slug,
  onBrowse,
}: {
  defaultSourcePath: string | null;
  slug: string | null;
  onBrowse: () => void;
}) {
  const { data: projects } = useProjects();
  const openPicker = useOpenFolderPicker();

  // Prefer the source path of the currently-selected project. Falls back to
  // the server's default-project hint, then the slug itself. The backend
  // resolves source_path from the most recent session's ``cwd`` when the
  // project was entered slug-only, so this is the real folder either way.
  const currentSourcePath =
    (slug && projects?.find((p) => p.slug === slug)?.source_path) ||
    defaultSourcePath;

  return (
    <header className="flex items-center gap-3 border-b border-border bg-card px-4 py-2">
      <button
        type="button"
        onClick={openPicker}
        className="text-sm font-semibold hover:underline"
        title="pick a folder"
      >
        unwind
      </button>
      <div className="flex min-w-0 items-center gap-1.5">
        <div className="truncate text-xs text-muted-foreground">
          {currentSourcePath ?? slug ?? "no project selected"}
        </div>
        <button
          type="button"
          onClick={openPicker}
          title="pick a different folder"
          className="inline-flex h-5 w-5 shrink-0 items-center justify-center rounded text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
        >
          <FolderSearch className="h-3.5 w-3.5" />
        </button>
        {slug && (
          <button
            type="button"
            onClick={onBrowse}
            title="browse known projects"
            className="inline-flex h-5 w-5 shrink-0 items-center justify-center rounded text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
          >
            <FolderTree className="h-3.5 w-3.5" />
          </button>
        )}
      </div>
      <div className="ml-auto text-[10px] text-muted-foreground">
        observer · read-only
      </div>
    </header>
  );
}
