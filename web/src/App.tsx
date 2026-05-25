import { useCallback, useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { pickFolder, useDefaultProject, useProjects } from "@/api/client";
import { useUi } from "@/store/ui";
import { isTypingTarget } from "@/lib/keyboard";
import { navigate, useUrlSync } from "@/lib/url-sync";
import { useLiveEvents } from "@/ws/client";
import {
  ResizableHandle,
  ResizablePanel,
  ResizablePanelGroup,
} from "@/components/ui/resizable";
import { SessionListPane } from "@/panes/SessionListPane";
import { CanvasPane } from "@/panes/CanvasPane";
import { ProjectPicker } from "@/panes/ProjectPicker";
import { ReportsPane } from "@/panes/ReportsPane";
import {
  BarChart3,
  FolderSearch,
  FolderTree,
  Telescope,
} from "lucide-react";

export function App() {
  const slug = useUi((s) => s.slug);
  const [showBrowser, setShowBrowser] = useState(false);
  // Reports is project-agnostic (cross-project month rollup). Its open/closed
  // state lives in the store so `lib/url-sync` mirrors it into the URL
  // (?view=reports&month=…); reachable whether or not a project is selected.
  const showReports = useUi((s) => s.reportsOpen);

  const { data: defaultProject } = useDefaultProject();
  useLiveEvents(slug);

  // Single source of truth for URL ↔ store sync: parses the URL on mount,
  // listens for popstate, and fills in the server's default project once
  // it loads (only when no project is already selected).
  useUrlSync(defaultProject?.slug ?? null);

  // Global ← / → to switch focus between panes; cmd/ctrl+O opens the folder
  // picker. Per-pane up/down handlers live in the panes themselves.
  const rotateFocus = useUi((s) => s.rotateFocus);
  const openPicker = useOpenFolderPicker();
  // focusedPane via ref so the keydown effect doesn't have to re-attach
  // on every focus change.
  const focusedPaneFromStore = useUi((s) => s.focusedPane);
  const focusedPaneRef = useRef(focusedPaneFromStore);
  focusedPaneRef.current = focusedPaneFromStore;
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
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
      if (isTypingTarget(e)) return;
      // When the canvas pane is focused, it owns ←/→ for tree
      // navigation (parent/child) and only falls through to pane focus
      // rotation when there's nowhere to go in that direction. Skipping
      // here lets the canvas handler decide.
      if (focusedPaneRef.current === "thread") return;
      if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
        e.preventDefault();
        // Prevent the canvas's own ←/→ handler from also running on
        // this event after we rotate focus into it — otherwise pressing
        // → from the sessions pane would BOTH switch panes AND jump the
        // canvas cursor to the root's first child in a single keystroke.
        e.stopImmediatePropagation();
        rotateFocus(e.key === "ArrowLeft" ? -1 : 1);
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
        onOpenReports={() => navigate.openReports()}
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
        {showReports && (
          <div
            className="absolute inset-0 z-30 bg-background/95 backdrop-blur-sm"
            // Mirrors the ProjectPicker overlay so click-outside-to-close
            // behaves consistently across overlays. Higher z so Reports
            // wins if both happen to be open at once.
            onMouseDown={(e) => {
              if (e.target === e.currentTarget) navigate.closeReports();
            }}
          >
            <ReportsPane onClose={() => navigate.closeReports()} />
          </div>
        )}
      </div>
    </div>
  );
}

const PANE_LABELS: Record<import("@/store/ui").PaneKey, string> = {
  sessions: "Session list",
  thread: "Canvas",
};

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
  // Focus signal lives in the pane background only — not the content —
  // so mouse-only users (who won't necessarily use ←/→ to switch focus)
  // aren't punished with dimmed cards. The focused pane keeps its full
  // ambient radial wash; the blurred pane drops the wash and shows a
  // flat surface (CSS rule on .uw-pane-blurred in index.css). A faint
  // inset ring on the focused pane adds an explicit accent.
  return (
    <div
      role="region"
      aria-label={PANE_LABELS[paneKey]}
      aria-keyshortcuts="ArrowUp ArrowDown ArrowLeft ArrowRight Enter Escape"
      tabIndex={0}
      onFocus={() => onFocus(paneKey)}
      onMouseDown={() => onFocus(paneKey)}
      className={
        "relative h-full transition-shadow outline-none focus-visible:ring-2 focus-visible:ring-primary/40 " +
        (focused
          ? "uw-pane-focused ring-1 ring-inset ring-primary/30"
          : "uw-pane-blurred")
      }
    >
      {children}
    </div>
  );
}

function useOpenFolderPicker() {
  const queryClient = useQueryClient();
  return useCallback(async () => {
    const result = await pickFolder();
    if (result.cancelled || !result.slug) return;
    // navigate.setSlug clears rootSessionId/detailSessionId/canvasFocus
    // and pushes a history entry so back returns to the previous project.
    navigate.setSlug(result.slug);
    // Refetch the project list so the title bar can resolve the new path.
    queryClient.invalidateQueries({ queryKey: ["projects"] });
  }, [queryClient]);
}

function TopBar({
  defaultSourcePath,
  slug,
  onBrowse,
  onOpenReports,
}: {
  defaultSourcePath: string | null;
  slug: string | null;
  onBrowse: () => void;
  onOpenReports: () => void;
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
        className="inline-flex items-center gap-1.5 text-sm font-semibold hover:underline"
        title="pick a folder"
      >
        <Telescope className="h-4 w-4" />
        unwind
      </button>
      <div className="text-xs text-muted-foreground">
        View Claude Code sessions with sub agent trees
      </div>
      <div className="ml-auto flex min-w-0 items-center gap-1.5">
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
        <button
          type="button"
          onClick={onOpenReports}
          title="usage reports"
          // Reports is project-agnostic so this button is always
          // available, even before a project is selected. Sits to the
          // right of the folder controls so the cluster reads as
          // "where am I" → "what did I spend".
          className="inline-flex h-5 w-5 shrink-0 items-center justify-center rounded text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
        >
          <BarChart3 className="h-3.5 w-3.5" />
        </button>
      </div>
    </header>
  );
}
