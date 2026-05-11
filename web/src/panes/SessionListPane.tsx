import { useEffect, useMemo, useRef, useState } from "react";
import { Search } from "lucide-react";
import { useSessions } from "@/api/client";
import type { SessionRow, SessionStatus } from "@/api/types";
import { useUi } from "@/store/ui";
import { navigate } from "@/lib/url-sync";
import { ScrollArea } from "@/components/ui/scroll-area";
import { cn, formatTimeAgo, shortId } from "@/lib/utils";

/** Forces re-render on a heartbeat so "X ago" labels stay fresh. */
function useTicker(ms: number = 5_000) {
  const [, set] = useState(0);
  useEffect(() => {
    const t = window.setInterval(() => set((n) => n + 1), ms);
    return () => window.clearInterval(t);
  }, [ms]);
}

function StatusDot({ status }: { status: SessionStatus }) {
  const cls =
    status === "live"
      ? "bg-emerald-500 animate-pulse"
      : status === "yield"
        ? "bg-amber-400 animate-pulse"
        : status === "idle"
          ? "bg-amber-500"
          : "bg-muted-foreground/40";
  return (
    <span
      className={cn("inline-block h-2 w-2 shrink-0 rounded-full", cls)}
      title={status}
    />
  );
}

export function SessionListPane() {
  const slug = useUi((s) => s.slug);
  const selectedId = useUi((s) => s.rootSessionId);
  const filter = useUi((s) => s.sessionFilter);
  const setFilter = useUi((s) => s.setSessionFilter);
  const { data, isLoading, error } = useSessions(slug, false);
  const searchRef = useRef<HTMLInputElement>(null);
  useTicker(5_000);

  // Auto-select the most recent session whenever the project changes
  // and nothing is selected yet (e.g., user just picked a new folder).
  // The URL-restore flow in App.tsx sets rootSessionId before sessions
  // load, so we won't trample a deep link. Use the *Auto variant — this
  // is canonicalization, not a user nav, so it doesn't push history.
  useEffect(() => {
    if (selectedId) return;
    if (!data || data.length === 0) return;
    navigate.selectRootSessionAuto(data[0].session_id);
  }, [data, selectedId]);

  const filtered = useMemo(() => {
    if (!data) return [];
    if (!filter.trim()) return data;
    const q = filter.toLowerCase();
    return data.filter(
      (s) =>
        s.title.toLowerCase().includes(q) ||
        s.session_id.toLowerCase().includes(q) ||
        (s.git_branch ?? "").toLowerCase().includes(q),
    );
  }, [data, filter]);

  const focusedPane = useUi((s) => s.focusedPane);

  // Keyboard: ↑/↓, j/k, AND Tab/Shift+Tab all move selection when this
  // pane is focused. (Tab/Shift+Tab is intercepted so the focus state
  // stays unified with the selection — there's no separate "tabbable
  // cursor" cycling through items.) `/` always focuses search.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      const typing =
        target &&
        (target.tagName === "INPUT" ||
          target.tagName === "TEXTAREA" ||
          target.isContentEditable);
      if (!typing && e.key === "/") {
        e.preventDefault();
        searchRef.current?.focus();
        return;
      }
      if (typing) return;
      const isArrow = e.key === "ArrowUp" || e.key === "ArrowDown";
      const isVim = e.key === "j" || e.key === "k";
      const isTab = e.key === "Tab";
      if (!isArrow && !isVim && !isTab) return;
      if (focusedPane !== "sessions") return;
      if (!filtered.length) return;
      e.preventDefault();
      const idx = filtered.findIndex((s) => s.session_id === selectedId);
      const goDown =
        e.key === "ArrowDown" ||
        e.key === "j" ||
        (e.key === "Tab" && !e.shiftKey);
      const next = goDown
        ? Math.min(filtered.length - 1, idx < 0 ? 0 : idx + 1)
        : Math.max(0, idx < 0 ? 0 : idx - 1);
      navigate.selectRootSession(filtered[next].session_id);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [filtered, selectedId, focusedPane]);

  return (
    <div className="uw-session-pane flex h-full flex-col">
      <header className="border-b border-border/60 px-3 py-3">
        <div className="flex items-baseline gap-2">
          <div className="text-[10px] font-bold uppercase tracking-[0.2em] text-muted-foreground">
            sessions
          </div>
          <div className="font-mono text-[10px] text-muted-foreground/70">
            {data ? `${filtered.length} / ${data.length}` : "—"}
          </div>
        </div>
        <div className="mt-3 flex items-center gap-1.5 rounded-xl border border-border/70 bg-background/60 px-3 py-1.5 text-xs focus-within:border-ring">
          <Search className="h-3 w-3 text-muted-foreground" />
          <input
            ref={searchRef}
            type="text"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="filter · /"
            className="w-full bg-transparent text-xs outline-none placeholder:text-muted-foreground"
          />
        </div>
      </header>
      <ScrollArea className="flex-1">
        {isLoading && (
          <div className="px-3 py-4 text-xs text-muted-foreground">loading…</div>
        )}
        {error && (
          <div className="px-3 py-4 text-xs text-destructive">
            {(error as Error).message}
          </div>
        )}
        {data && data.length === 0 && (
          <div className="px-3 py-4 text-xs text-muted-foreground">
            no sessions yet — run Claude Code in this folder.
          </div>
        )}
        <ul className="py-1">
          {filtered.map((s) => (
            <SessionItem
              key={s.session_id}
              session={s}
              selected={s.session_id === selectedId}
              onSelect={() => navigate.selectRootSession(s.session_id)}
            />
          ))}
        </ul>
      </ScrollArea>
    </div>
  );
}

function SessionItem({
  session,
  selected,
  onSelect,
}: {
  session: SessionRow;
  selected: boolean;
  onSelect: () => void;
}) {
  const startedAgo = formatTimeAgo(session.first_timestamp);
  const updatedAgo = formatTimeAgo(session.last_timestamp);
  const sameTimes = session.first_timestamp === session.last_timestamp;

  // Scroll the selected row into view, AND move keyboard focus to it.
  // Selection IS the focus state — there's no separate "tabbable
  // cursor" cycling through unselected items.
  const btnRef = useRef<HTMLButtonElement | null>(null);
  useEffect(() => {
    if (!selected) return;
    btnRef.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    btnRef.current?.focus({ preventScroll: true });
  }, [selected]);

  return (
    <li>
      <button
        ref={btnRef}
        type="button"
        // Only the selected row is tabbable — unselected rows have
        // ``tabIndex=-1`` so Tab/Shift+Tab won't cycle through them.
        // The pane-level keydown handler intercepts Tab/Shift+Tab and
        // moves selection (which moves focus, since they're unified).
        tabIndex={selected ? 0 : -1}
        onClick={onSelect}
        className={cn(
          // Flat list row: background-only hover/selection treatment so
          // the list reads as one continuous surface, not a stack of
          // boxes. A 2px accent bar on the left edge marks the selected
          // row without thickening the row's footprint. Yield is
          // signaled solely by the StatusDot — no row-level wash.
          "relative flex w-full flex-col gap-1 px-4 py-2 text-left transition-colors",
          // Suppress the browser's default focus outline — selection
          // (the left-edge bar + bg) is the focus indicator.
          "outline-none focus:outline-none focus-visible:outline-none",
          "before:pointer-events-none before:absolute before:inset-y-1 before:left-0 before:w-[2px] before:rounded-r before:bg-transparent before:transition-colors",
          "hover:bg-foreground/[0.04]",
          selected && "bg-foreground/[0.07] before:bg-primary",
        )}
      >
        <div className="flex items-center gap-2">
          <StatusDot status={session.status ?? "done"} />
          <span className="flex-1 truncate text-[13px] text-foreground">
            {session.title || shortId(session.session_id)}
          </span>
        </div>
        <div className="flex flex-col gap-0.5 pl-4 text-[10px] leading-tight text-muted-foreground">
          <span title={session.first_timestamp ?? ""}>
            started <span className="text-foreground/80">{startedAgo}</span>
            {!sameTimes && (
              <>
                {" · updated "}
                <span className="text-foreground/80">{updatedAgo}</span>
              </>
            )}
          </span>
          <span className="flex items-center gap-1.5">
            <span className="font-mono">{shortId(session.session_id)}</span>
            <span className="opacity-40">·</span>
            <span>{session.message_count} msgs</span>
            {session.top_level_call_count > 0 ? (
              <>
                <span className="opacity-40">·</span>
                <span>
                  {session.top_level_call_count}{" "}
                  {session.top_level_call_count === 1 ? "call" : "calls"}
                </span>
              </>
            ) : null}
            {session.git_branch ? (
              <>
                <span className="opacity-40">·</span>
                <span className="truncate">{session.git_branch}</span>
              </>
            ) : null}
          </span>
        </div>
      </button>
    </li>
  );
}
