import { useEffect, useMemo, useRef, useState } from "react";
import { Search } from "lucide-react";
import { useSessions } from "@/api/client";
import type { SessionRow, SessionStatus } from "@/api/types";
import { useUi } from "@/store/ui";
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
  const select = useUi((s) => s.selectRootSession);
  const filter = useUi((s) => s.sessionFilter);
  const setFilter = useUi((s) => s.setSessionFilter);
  const showForks = useUi((s) => s.showForks);
  const setShowForks = useUi((s) => s.setShowForks);

  const { data, isLoading, error } = useSessions(slug, showForks);
  const searchRef = useRef<HTMLInputElement>(null);
  useTicker(5_000);

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

  // Keyboard: ↑/↓ + j/k to move between sessions when this pane is focused;
  // `/` always focuses search.
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
      if (!isArrow && !isVim) return;
      if (isArrow && focusedPane !== "sessions") return;
      if (!filtered.length) return;
      e.preventDefault();
      const idx = filtered.findIndex((s) => s.session_id === selectedId);
      const goDown = e.key === "ArrowDown" || e.key === "j";
      const next = goDown
        ? Math.min(filtered.length - 1, idx < 0 ? 0 : idx + 1)
        : Math.max(0, idx < 0 ? 0 : idx - 1);
      select(filtered[next].session_id);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [filtered, selectedId, select, focusedPane]);

  return (
    <div className="flex h-full flex-col">
      <header className="border-b border-border px-3 py-2">
        <div className="flex items-center justify-between gap-2">
          <div>
            <div className="text-[11px] uppercase tracking-wider text-muted-foreground">
              sessions
            </div>
            <div className="text-xs text-muted-foreground">
              {data ? `${filtered.length} / ${data.length}` : "—"}
              {!showForks ? " · forks hidden" : ""}
            </div>
          </div>
          <label
            className="flex items-center gap-1.5 text-[10px] text-muted-foreground"
            title="Show callstack-forked child sessions in this list"
          >
            <input
              type="checkbox"
              checked={showForks}
              onChange={(e) => setShowForks(e.target.checked)}
              className="h-3 w-3"
            />
            forks
          </label>
        </div>
        <div className="mt-2 flex items-center gap-1.5 rounded-md border border-border bg-background px-2 py-1 text-xs focus-within:border-ring">
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
              onSelect={() => select(s.session_id)}
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
  const isYield = session.status === "yield";

  // Scroll the selected row into view (only if not already visible) so
  // keyboard navigation, URL-deep-links, and long lists all keep the
  // current item on screen.
  const btnRef = useRef<HTMLButtonElement | null>(null);
  useEffect(() => {
    if (!selected) return;
    btnRef.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [selected]);

  return (
    <li>
      <button
        ref={btnRef}
        type="button"
        onClick={onSelect}
        className={cn(
          "flex w-full flex-col gap-1 border-l-2 border-transparent px-3 py-2 text-left transition-colors",
          "hover:bg-accent/60",
          selected && "bg-accent border-l-primary",
          isYield &&
            "bg-amber-500/25 hover:bg-amber-500/30 border-l-amber-500",
          isYield && selected && "bg-amber-500/40 hover:bg-amber-500/40",
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
