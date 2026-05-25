import { useEffect, useMemo, useState } from "react";
import { ChevronLeft, ChevronRight, DollarSign, X } from "lucide-react";
import { useUsageReport } from "@/api/client";
import type {
  ProjectGroupRow,
  ProjectUsageRow,
  TokenCost,
  TokenUsage,
} from "@/api/types";
import { ScrollArea } from "@/components/ui/scroll-area";

/** Monthly cross-project token + USD report. Renders the same shape as
 *  ``unwind usage report``: a KPI strip at the top, a per-category
 *  breakdown, and a per-project table with top-N rows + ephemeral
 *  rollup + tail rollup + grand total.
 *
 *  Reports is project-agnostic so this view lives at the App level
 *  (toggled from the TopBar), not under a project's pane group. */
export function ReportsPane({ onClose }: { onClose?: () => void } = {}) {
  const [month, setMonth] = useState<string>(() => currentLocalMonth());
  const { data, isLoading, error } = useUsageReport(month, 20);

  const monthLabel = useMemo(() => formatMonth(month), [month]);

  // Esc closes the overlay — matches the node-detail dismiss convention
  // (see CanvasPane). Listener is attached only when onClose is
  // provided so the component stays embeddable without a close
  // handler (e.g. if a future route makes Reports its own page).
  useEffect(() => {
    if (!onClose) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="mx-auto flex h-full w-full max-w-6xl flex-col">
      <header className="flex items-start justify-between gap-3 px-6 pt-8 pb-4">
        <div>
          <div className="inline-flex items-center gap-2 text-xl font-semibold">
            <DollarSign className="h-5 w-5" />
            Usage report
          </div>
          <div className="mt-1 text-xs text-muted-foreground">
            Token usage and USD cost across every known project. A token
            is counted in a month when the assistant turn that produced
            it falls in that local calendar month
            {data?.tz_name ? ` (${data.tz_name})` : ""}.
          </div>
        </div>
        <div className="flex items-center gap-2">
          <MonthPicker month={month} onChange={setMonth} label={monthLabel} />
          {onClose && (
            <button
              type="button"
              onClick={onClose}
              title="close (Esc)"
              aria-label="close usage report"
              // Mirrors CanvasPane's detail dismiss: an icon + text
              // button rather than a tiny bare X. The kbd hint to the
              // right reinforces Esc.
              className="inline-flex items-center gap-1 rounded border border-border bg-card px-2 py-1 text-xs text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
            >
              <X className="h-3.5 w-3.5" />
              close
              <kbd className="ml-1 rounded border border-border bg-muted px-1 text-[10px]">
                esc
              </kbd>
            </button>
          )}
        </div>
      </header>

      <ScrollArea className="flex-1 px-6 pb-8">
        {isLoading && (
          <div className="py-8 text-sm text-muted-foreground">loading…</div>
        )}
        {error && (
          <div className="py-8 text-sm text-destructive">
            failed to load report: {String(error)}
          </div>
        )}
        {data && (
          <>
            <KpiStrip data={data} />
            <CategoryBreakdown
              usage={data.grand_usage}
              cost={data.grand_cost}
              total={data.total_cost}
            />
            <ProjectTable
              top={data.buckets.top}
              ephemeral={data.buckets.ephemeral}
              other={data.buckets.other}
              grandUsage={data.grand_usage}
              grandCost={data.grand_cost}
              grandSessions={data.session_count}
              grandTotal={data.total_cost}
            />
          </>
        )}
      </ScrollArea>
    </div>
  );
}

// --- Pieces -----------------------------------------------------------

function MonthPicker({
  month,
  onChange,
  label,
}: {
  month: string;
  onChange: (m: string) => void;
  label: string;
}) {
  return (
    <div className="inline-flex items-center gap-1 rounded border border-border px-1 py-0.5">
      <button
        type="button"
        title="previous month"
        onClick={() => onChange(shiftMonth(month, -1))}
        className="inline-flex h-6 w-6 items-center justify-center rounded text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
      >
        <ChevronLeft className="h-4 w-4" />
      </button>
      <input
        type="month"
        value={month}
        onChange={(e) => onChange(e.target.value || month)}
        aria-label="month"
        className="h-6 bg-transparent px-1 text-xs font-mono text-foreground outline-none"
        // ``type=month`` shows a native picker; the text label below is a
        // human-readable fallback for browsers without month-picker UI.
      />
      <span className="hidden text-xs text-muted-foreground sm:inline">
        {label}
      </span>
      <button
        type="button"
        title="next month"
        onClick={() => onChange(shiftMonth(month, 1))}
        className="inline-flex h-6 w-6 items-center justify-center rounded text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
      >
        <ChevronRight className="h-4 w-4" />
      </button>
    </div>
  );
}

function KpiStrip({
  data,
}: {
  data: {
    total_cost: number;
    total_tokens: number;
    session_count: number;
    project_count: number;
    month: string;
    tz_name: string;
  };
}) {
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
      <Kpi label="Total cost" value={fmtUsd(data.total_cost)} accent="emerald" />
      <Kpi label="Total tokens" value={fmtTokens(data.total_tokens)} />
      <Kpi label="Sessions" value={data.session_count.toLocaleString()} />
      <Kpi label="Projects" value={data.project_count.toLocaleString()} />
    </div>
  );
}

function Kpi({
  label,
  value,
  accent,
}: {
  label: string;
  value: string;
  accent?: "emerald";
}) {
  return (
    <div className="rounded border border-border bg-card p-3">
      <div className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
        {label}
      </div>
      <div
        className={
          "mt-1 font-mono text-2xl tabular-nums " +
          (accent === "emerald" ? "text-emerald-400" : "text-foreground")
        }
      >
        {value}
      </div>
    </div>
  );
}

function CategoryBreakdown({
  usage,
  cost,
  total,
}: {
  usage: TokenUsage;
  cost: TokenCost;
  total: number;
}) {
  const rows: { key: keyof TokenUsage; label: string }[] = [
    { key: "cw", label: "Cache write" },
    { key: "cr", label: "Cache read" },
    { key: "r", label: "Input" },
    { key: "w", label: "Output" },
  ];
  return (
    <div className="mt-4 rounded border border-border bg-card p-3">
      <div className="mb-2 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
        Cost by category
      </div>
      <table className="w-full text-xs tabular-nums">
        <tbody>
          {rows.map(({ key, label }) => {
            const pct = total > 0 ? (cost[key] / total) * 100 : 0;
            return (
              <tr key={key} className="border-t border-border/40">
                <td className="py-1 pr-3 text-muted-foreground">{label}</td>
                <td className="w-24 py-1 pr-3 text-right font-mono">
                  {fmtTokens(usage[key])}
                </td>
                <td className="w-24 py-1 pr-3 text-right font-mono">
                  {fmtUsd(cost[key])}
                </td>
                <td className="py-1">
                  <div className="h-1.5 w-full overflow-hidden rounded bg-muted">
                    <div
                      className="h-full bg-emerald-500/60"
                      style={{ width: `${pct.toFixed(2)}%` }}
                    />
                  </div>
                </td>
                <td className="w-12 py-1 pl-2 text-right text-muted-foreground">
                  {pct.toFixed(0)}%
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function ProjectTable({
  top,
  ephemeral,
  other,
  grandUsage,
  grandCost,
  grandSessions,
  grandTotal,
}: {
  top: ProjectUsageRow[];
  ephemeral: ProjectGroupRow | null;
  other: ProjectGroupRow | null;
  grandUsage: TokenUsage;
  grandCost: TokenCost;
  grandSessions: number;
  grandTotal: number;
}) {
  return (
    <div className="mt-4 rounded border border-border bg-card">
      <table className="w-full text-xs tabular-nums">
        <thead>
          <tr className="border-b border-border bg-muted/30 text-[10px] uppercase tracking-wider text-muted-foreground">
            <th className="px-3 py-2 text-left font-medium">Project</th>
            <th className="px-2 py-2 text-right font-medium">Sess</th>
            <th className="px-2 py-2 text-right font-medium">Cache W</th>
            <th className="px-2 py-2 text-right font-medium">Cache R</th>
            <th className="px-2 py-2 text-right font-medium">In</th>
            <th className="px-2 py-2 text-right font-medium">Out</th>
            <th className="px-3 py-2 text-right font-medium">Cost</th>
          </tr>
        </thead>
        <tbody>
          {top.map((p) => (
            <ProjectRow
              key={p.slug}
              name={friendlyName(p.slug, p.source_path)}
              sub={p.source_path}
              sessions={p.session_count}
              usage={p.usage}
              total={p.total_cost}
            />
          ))}
          {ephemeral && (
            <ProjectRow
              key="__ephemeral__"
              name={ephemeral.label}
              sub="rolled up — /tmp and /private/tmp paths"
              sessions={ephemeral.session_count}
              usage={ephemeral.usage}
              total={ephemeral.total_cost}
              muted
            />
          )}
          {other && (
            <ProjectRow
              key="__other__"
              name={other.label}
              sub="rolled up — long tail past top 20"
              sessions={other.session_count}
              usage={other.usage}
              total={other.total_cost}
              muted
            />
          )}
        </tbody>
        <tfoot>
          <tr className="border-t-2 border-border bg-muted/30 font-semibold">
            <td className="px-3 py-2 text-left">Total</td>
            <td className="px-2 py-2 text-right">{grandSessions.toLocaleString()}</td>
            <td className="px-2 py-2 text-right font-mono">{fmtTokens(grandUsage.cw)}</td>
            <td className="px-2 py-2 text-right font-mono">{fmtTokens(grandUsage.cr)}</td>
            <td className="px-2 py-2 text-right font-mono">{fmtTokens(grandUsage.r)}</td>
            <td className="px-2 py-2 text-right font-mono">{fmtTokens(grandUsage.w)}</td>
            <td className="px-3 py-2 text-right font-mono text-emerald-400">
              {fmtUsd(grandTotal)}
            </td>
          </tr>
        </tfoot>
      </table>
      <div className="border-t border-border px-3 py-1.5 text-[10px] text-muted-foreground">
        Grand total sums every event in the window, including ephemeral
        test runs and the long tail. Category totals are{" "}
        <span className="font-mono">
          ${grandCost.cw.toFixed(0)} CW · ${grandCost.cr.toFixed(0)} CR ·
          ${grandCost.r.toFixed(0)} In · ${grandCost.w.toFixed(0)} Out
        </span>
        .
      </div>
    </div>
  );
}

function ProjectRow({
  name,
  sub,
  sessions,
  usage,
  total,
  muted,
}: {
  name: string;
  sub: string;
  sessions: number;
  usage: TokenUsage;
  total: number;
  muted?: boolean;
}) {
  return (
    <tr
      className={
        "border-t border-border/40 " +
        (muted ? "text-muted-foreground" : "")
      }
    >
      <td className="px-3 py-1.5">
        <div className="font-medium">{name}</div>
        <div className="truncate text-[10px] text-muted-foreground/80">
          {sub}
        </div>
      </td>
      <td className="px-2 py-1.5 text-right">{sessions.toLocaleString()}</td>
      <td className="px-2 py-1.5 text-right font-mono">{fmtTokens(usage.cw)}</td>
      <td className="px-2 py-1.5 text-right font-mono">{fmtTokens(usage.cr)}</td>
      <td className="px-2 py-1.5 text-right font-mono">{fmtTokens(usage.r)}</td>
      <td className="px-2 py-1.5 text-right font-mono">{fmtTokens(usage.w)}</td>
      <td className="px-3 py-1.5 text-right font-mono">{fmtUsd(total)}</td>
    </tr>
  );
}

// --- Helpers ----------------------------------------------------------
// Token/USD formatters intentionally duplicated from UsageFooter.tsx.
// Both are 5 lines, and extracting to a shared module would mean
// editing UsageFooter (unrelated to this change). If/when a third
// caller needs them, consolidate then.

function fmtUsd(n: number): string {
  if (!n) return "$0.00";
  if (n < 0.01 && n > 0) return "<$0.01";
  return `$${n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function fmtTokens(n: number): string {
  if (!n) return "0";
  if (n >= 1_000_000_000) return `${(n / 1_000_000_000).toFixed(2).replace(/\.?0+$/, "")}B`;
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1).replace(/\.0$/, "")}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1).replace(/\.0$/, "")}K`;
  return String(n);
}

/** ``YYYY-MM`` in the user's local timezone. */
function currentLocalMonth(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

function shiftMonth(month: string, delta: number): string {
  const [y, m] = month.split("-").map(Number);
  const d = new Date(y, m - 1 + delta, 1);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

function formatMonth(month: string): string {
  const [y, m] = month.split("-").map(Number);
  return new Date(y, m - 1, 1).toLocaleString("en-US", {
    month: "long",
    year: "numeric",
  });
}

/** Project name for display. ``source_path`` is the project's real
 *  working directory when explicitly registered, or a synthesized
 *  ``~/.claude/projects/<slug>`` when only discovered on disk. In the
 *  synthesized case we reverse-translate the slug to a path (lossy
 *  but readable) — same logic as the CLI renderer. */
function friendlyName(slug: string, sourcePath: string): string {
  const path = sourcePath.includes("/.claude/projects/")
    ? "/" + slug.replace(/^-/, "").replace(/-/g, "/")
    : sourcePath.replace(/\/+$/, "");
  const parts = path.split("/").filter(Boolean);
  if (parts.length >= 2) return `${parts[parts.length - 2]}/${parts[parts.length - 1]}`;
  return parts[parts.length - 1] || slug;
}
