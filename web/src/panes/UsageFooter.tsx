import type { ReactNode } from "react";
import { Box, DollarSign, Network } from "lucide-react";
import type { TokenCost, TokenUsage } from "@/api/types";

/** Footer with abbreviated token-usage counters and optional $ cost.
 *
 *  Layout:
 *  - One <table> with a header row (``Cache Write`` / ``Cache Read`` /
 *    ``Read`` / ``Write``) above four data columns. The leftmost column
 *    is a narrow icon cell — single-node icon for ``Self``, network
 *    icon for ``Subtree``, dollar icon for the root's $ row — so we
 *    stay at "4 data columns" without burning width on text labels.
 *  - The root card additionally renders a ``$N.NN total`` line below
 *    the table summing the subtree cost.
 *
 *  Font-size 80% (≈ -2px from the footer's 10px base) is applied to the
 *  table itself so headers, data, and the icon column all scale
 *  together. */
export function UsageFooter({
  self,
  subtree,
  subtreeCost,
  showSubtree,
  showCost,
}: {
  self: TokenUsage;
  subtree: TokenUsage;
  subtreeCost: TokenCost;
  showSubtree: boolean;
  showCost: boolean;
}) {
  // Hide the footer entirely when nothing has been logged yet (e.g. a
  // brand-new live session whose first assistant message hasn't been
  // written). Avoids a row of `0 0 0 0` on empty intermediate cards.
  // EXCEPTION: the root card always shows so the $ total line stays
  // visible even on sessions with no assistant turns yet (legitimate
  // for live sessions still waiting on the first reply).
  const empty = (u: TokenUsage) => !u.cw && !u.cr && !u.r && !u.w;
  const selfEmpty = empty(self);
  const subtreeEmpty = !showSubtree || empty(subtree);
  if (!showCost && selfEmpty && subtreeEmpty) {
    return null;
  }
  const costTotal =
    subtreeCost.cw + subtreeCost.cr + subtreeCost.r + subtreeCost.w;
  return (
    <footer className="border-t border-border/60 px-4 py-2 font-mono text-[10px] text-muted-foreground/90">
      <table className="w-full tabular-nums" style={{ fontSize: "80%" }}>
        <thead>
          <tr className="text-[9px] font-medium uppercase tracking-[0.08em] text-muted-foreground/60">
            <th className="w-4" />
            <th className="text-right font-medium">Cache Write</th>
            <th className="text-right font-medium">Cache Read</th>
            <th className="text-right font-medium">Read</th>
            <th className="text-right font-medium">Write</th>
          </tr>
        </thead>
        <tbody>
          <UsageRow
            icon={<Box className="h-3 w-3" aria-label="self" />}
            u={self}
          />
          {showSubtree ? (
            <UsageRow
              icon={<Network className="h-3 w-3" aria-label="subtree" />}
              u={subtree}
            />
          ) : null}
          {showCost ? <CostRow c={subtreeCost} /> : null}
        </tbody>
      </table>
      {showCost ? (
        <div className="mt-1 text-right tabular-nums text-emerald-300/90">
          {fmtUsd(costTotal)} total
        </div>
      ) : null}
    </footer>
  );
}

function UsageRow({ icon, u }: { icon: ReactNode; u: TokenUsage }) {
  return (
    <tr>
      <td className="w-4 pr-1 text-muted-foreground/70">{icon}</td>
      <td className="text-right">{fmtTokens(u.cw)}</td>
      <td className="text-right">{fmtTokens(u.cr)}</td>
      <td className="text-right">{fmtTokens(u.r)}</td>
      <td className="text-right">{fmtTokens(u.w)}</td>
    </tr>
  );
}

/** Per-category $ row on the root card. Uses the same 4-column layout
 *  as the token rows so the dollar amounts align vertically with their
 *  corresponding token counts. The grand total lives on its own line
 *  below the table. */
function CostRow({ c }: { c: TokenCost }) {
  return (
    <tr className="text-emerald-300/90">
      <td className="w-4 pr-1">
        <DollarSign className="h-3 w-3" aria-label="cost" />
      </td>
      <td className="text-right">{fmtUsd(c.cw)}</td>
      <td className="text-right">{fmtUsd(c.cr)}</td>
      <td className="text-right">{fmtUsd(c.r)}</td>
      <td className="text-right">{fmtUsd(c.w)}</td>
    </tr>
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
