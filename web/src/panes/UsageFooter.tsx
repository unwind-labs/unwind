import { DollarSign, Network, Telescope } from "lucide-react";
import type { TokenCost, TokenUsage } from "@/api/types";

/** Footer with abbreviated token-usage counters and $ cost.
 *
 *  Layout (transposed): token categories run DOWN as rows, scopes
 *  (``Here`` / ``Sub-calls`` / ``$``) run ACROSS as columns. The narrow
 *  card is 380px wide — 4 category rows × up-to-3 scope columns reads
 *  more naturally than 3 scope rows × 4 category columns (the older
 *  layout pushed numbers into the tight right edge).
 *
 *  ``isRoot`` forces the footer to render even when every counter is
 *  zero — the root's grand-total $ line stays visible on brand-new
 *  sessions whose first assistant message hasn't been written yet.
 *  Non-root cards in that state hide entirely (no point showing a sea
 *  of zeros). */
export function UsageFooter({
  self,
  subtree,
  subtreeCost,
  showSubtree,
  isRoot,
}: {
  self: TokenUsage;
  subtree: TokenUsage;
  subtreeCost: TokenCost;
  showSubtree: boolean;
  isRoot: boolean;
}) {
  const empty = (u: TokenUsage) => !u.cw && !u.cr && !u.r && !u.w;
  const selfEmpty = empty(self);
  const subtreeEmpty = !showSubtree || empty(subtree);
  if (!isRoot && selfEmpty && subtreeEmpty) {
    return null;
  }
  const costTotal = subtreeCost.cw + subtreeCost.cr + subtreeCost.r + subtreeCost.w;
  const categories: { key: keyof TokenUsage; label: string }[] = [
    { key: "cw", label: "Cache Write" },
    { key: "cr", label: "Cache Read" },
    { key: "r", label: "Read" },
    { key: "w", label: "Write" },
  ];
  return (
    <footer className="border-t border-border/60 px-4 py-2 font-mono text-[10px] text-muted-foreground/90">
      <table className="w-full tabular-nums" style={{ fontSize: "80%" }}>
        <thead>
          <tr className="text-[9px] font-medium uppercase tracking-[0.08em] text-muted-foreground/60">
            <th className="text-left font-medium" />
            <th className="pl-2 text-right font-medium">
              <span className="inline-flex items-center justify-end">
                <Telescope className="h-3.5 w-3.5" aria-label="this window" />
              </span>
            </th>
            {showSubtree ? (
              <th className="pl-2 text-right font-medium">
                <span className="inline-flex items-center justify-end">
                  <Network
                    className="h-3.5 w-3.5 -rotate-90"
                    aria-label="this window + sub-calls"
                  />
                </span>
              </th>
            ) : null}
            <th className="pl-2 text-right font-medium text-emerald-300/90">
              <span className="inline-flex items-center justify-end">
                <DollarSign className="h-3.5 w-3.5" aria-label="cost" />
              </span>
            </th>
          </tr>
        </thead>
        <tbody>
          {categories.map(({ key, label }) => (
            <tr key={key}>
              <td className="text-left text-muted-foreground/70">{label}</td>
              <td className="pl-2 text-right">{fmtTokens(self[key])}</td>
              {showSubtree ? <td className="pl-2 text-right">{fmtTokens(subtree[key])}</td> : null}
              <td className="pl-2 text-right text-emerald-300/90">{fmtUsd(subtreeCost[key])}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="mt-1 text-right tabular-nums text-emerald-300/90">{fmtUsd(costTotal)}</div>
    </footer>
  );
}

/** Format USD: ``$0.00`` for ≥ $0.01, ``<$0.01`` for tiny non-zero
 *  amounts (so a real cost never displays as $0.00). Comma-grouped above
 *  $1000 so totals on long-running roots stay readable. */
function fmtUsd(n: number): string {
  if (!n) return "$0.00";
  if (n < 0.01) return "<$0.01";
  return `$${n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

/** Abbreviated counter: ``1234`` → ``1.2K``, ``1_500_000`` → ``1.5M``.
 *  Trailing ``.0`` is stripped so round numbers stay tight at 380px width. */
function fmtTokens(n: number): string {
  if (!n) return "0";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1).replace(/\.0$/, "")}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1).replace(/\.0$/, "")}K`;
  return String(n);
}
