/**
 * The established V0.2 limits, and how much of each is in use.
 *
 * The limits are read from the risk engine itself, so this panel cannot drift
 * from the policy it describes. They are always shown - a limit does not stop
 * existing because the account behind it is unreadable - while utilization is
 * an observation and says so when it is missing.
 *
 * A row that cannot be measured says why **once**, under an empty track.
 * Repeating the same reason beside every figure in the row turned three
 * unreadable numbers into nine words of noise and made the card harder to
 * read than the state it was describing.
 *
 * The bars are read-only, like everything else here. There is no control that
 * edits a limit, and no endpoint that would accept one.
 */

import { money, percent, unavailableLabel } from "@/lib/format";
import type { ExposureRow, RiskLimit, RiskPanel } from "@/lib/types";

import { Bar, Card, Tag, cn } from "./ui";

function Limit({ limit }: { limit: RiskLimit }) {
  const known = limit.used_value.available && limit.used_fraction !== null;
  return (
    <div className="py-3 first:pt-0 last:pb-0">
      <div className="flex items-baseline justify-between gap-3">
        <div className="flex min-w-0 items-baseline gap-2">
          <span className="truncate text-[12.5px] text-ink-2" title={limit.detail ?? undefined}>
            {limit.label}
          </span>
          {limit.subject ? <Tag>{limit.subject}</Tag> : null}
        </div>
        <span className="num shrink-0 text-[12.5px] whitespace-nowrap">
          <span className={cn(known ? (limit.breached ? "text-neg" : "text-ink") : "text-ink-3")}>
            {known ? percent(limit.used_fraction) : "—"}
          </span>
          <span className="text-ink-3"> / {percent(limit.limit_fraction, 0)}</span>
        </span>
      </div>

      <div className="mt-2">
        <Bar value={limit.utilization} breached={limit.breached} />
      </div>

      <div className="mt-1.5 text-[11px] whitespace-nowrap text-ink-3">
        {known ? (
          <span className="num">
            {money(limit.used_value.value)} of {money(limit.limit_value.value)}
          </span>
        ) : (
          unavailableLabel(limit.used_value.unavailable_reason)
        )}
      </div>
    </div>
  );
}

/**
 * Where total exposure sits, split by book.
 *
 * This is a **breakdown of one enforced number**, not two limits. Crypto and
 * equity draw on the same 30% account cap; there is no per-book allocation in
 * the risk engine, and putting a "crypto limit" on this screen would name a
 * rule that nothing enforces. Only the total row carries a cap, and only the
 * total row gets a bar - the two book rows are figures, not tracks against a
 * ceiling, and giving them tracks is exactly how a reader would misread them.
 */
function Exposure({ rows, limitFraction }: { rows: ExposureRow[]; limitFraction: number | null }) {
  if (rows.length === 0) return null;
  const total = rows.find((row) => row.enforced) ?? null;
  const books = rows.filter((row) => !row.enforced);

  return (
    <div className="mt-3 border-t border-line pt-3">
      <h3 className="eyebrow text-ink-3">Exposure by book</h3>
      <div className="mt-2 space-y-1.5">
        {books.map((row) => (
          <div key={row.key} className="flex items-baseline justify-between gap-3">
            <span className="text-[12.5px] text-ink-2">{row.label}</span>
            <span className="num shrink-0 text-[12.5px] whitespace-nowrap">
              {row.value.available ? (
                <>
                  <span className="text-ink">{percent(row.fraction)}</span>
                  <span className="text-ink-3"> · {money(row.value.value)}</span>
                </>
              ) : (
                <span className="text-ink-3">—</span>
              )}
            </span>
          </div>
        ))}
        {total ? (
          <div className="flex items-baseline justify-between gap-3 border-t border-line pt-1.5">
            <span className="text-[12.5px] font-medium text-ink">{total.label}</span>
            <span className="num shrink-0 text-[12.5px] whitespace-nowrap">
              <span className="text-ink">
                {total.value.available ? percent(total.fraction) : "—"}
              </span>
              <span className="text-ink-3"> / {percent(limitFraction, 0)} account</span>
            </span>
          </div>
        ) : null}
      </div>
      <p className="mt-2 text-[11px] leading-snug text-ink-3">
        One account, one 30% cap. There is no per-book limit - this is where the total sits, not
        an allocation between the two.
      </p>
    </div>
  );
}

export function Risk({ panel }: { panel: RiskPanel | null }) {
  if (!panel) {
    return (
      <Card title="Risk limits">
        <p className="text-[12px] text-ink-3">Risk state could not be read.</p>
      </Card>
    );
  }

  return (
    <Card
      title="Risk limits"
      meta={<Tag title="The limits enforced by autotrader.risk.engine.">V0.2 policy</Tag>}
      bodyClassName="px-4 py-3.5"
    >
      <div className="divide-y divide-line">
        {panel.limits.map((limit) => (
          <Limit key={limit.key} limit={limit} />
        ))}
      </div>
      <Exposure rows={panel.exposure} limitFraction={panel.total_exposure_limit_fraction} />
      {panel.available ? null : (
        <p className="mt-3 border-t border-line pt-3 text-[11px] leading-snug text-ink-3">
          Limits are policy and are always shown. Current utilization needs a broker read.
        </p>
      )}
    </Card>
  );
}
