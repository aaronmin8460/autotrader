/**
 * The shared account safety state, and the shared API budget beside it.
 *
 * There is one brokerage account, so there is one answer to "may anything
 * submit an order right now?" - and it is deliberately not folded into either
 * runtime card. An ambiguous order raised by the equity service stops the
 * crypto service too, and a screen that showed that as a property of one
 * runtime would be describing the wrong thing.
 *
 * When the account is halted this is the most important thing on the page, so
 * it is a full-width strip above the runtimes rather than a card in a column.
 * When it is safe it is one quiet line.
 *
 * The `client_order_id` is shown for a halt caused by an unknown outcome
 * because it is the recovery anchor: it names the exact key to ask the broker
 * about. It is an identifier this system generated, never a credential.
 *
 * Read-only, like everything else. There is no clear-the-halt control here and
 * no endpoint that would accept one - the halt is cleared by a full-universe
 * reconciliation run from the CLI, and by nothing else.
 */

import { relative, stampUtc } from "@/lib/format";
import type { AccountSafetyPanel, ApiBudgetRow } from "@/lib/types";

import { Card, Dot, Field, Tag, cn, toneText } from "./ui";

function BudgetLine({ row }: { row: ApiBudgetRow }) {
  return (
    <Field
      label={row.label}
      title="Counted across both runtimes. The ceiling is this system's own, not a provider limit."
    >
      <span className="num">
        <span className={row.remaining === 0 ? "text-warn" : "text-ink"}>{row.used}</span>
        <span className="text-ink-3"> / {row.limit} this minute</span>
      </span>
    </Field>
  );
}

export function AccountSafety({
  panel,
  budget,
  lastFailure,
  lastFailureAt,
  generatedAt,
}: {
  panel: AccountSafetyPanel | null;
  budget: ApiBudgetRow[];
  lastFailure: string | null;
  lastFailureAt: string | null;
  generatedAt: string | null;
}) {
  if (!panel) {
    return (
      <Card title="Account safety">
        <p className="text-[12px] text-ink-3">Shared account safety could not be read.</p>
      </Card>
    );
  }

  const meta = (
    <>
      <Tag title="One Alpaca paper account carries both books.">Shared</Tag>
      <span
        className={cn(
          "inline-flex items-center gap-1.5 text-[11px] leading-none font-medium",
          "tracking-[0.06em] uppercase",
          toneText(panel.tone),
        )}
      >
        <Dot tone={panel.tone} />
        {panel.state}
      </span>
    </>
  );

  return (
    <Card title="Account safety" meta={meta} bodyClassName="">
      <div className="px-4 py-3.5">
        <p
          className={cn(
            "text-[12.5px] leading-snug",
            panel.safe_to_trade ? "text-ink-2" : "text-ink",
          )}
        >
          {panel.detail}
        </p>
        {panel.client_order_id ? (
          <p className="num mt-2 text-[11.5px] text-ink-2">
            <span className="text-ink-3">Unresolved client_order_id: </span>
            {panel.client_order_id}
          </p>
        ) : null}
        {panel.safe_to_trade ? null : (
          <p className="mt-2 text-[11px] leading-snug text-ink-3">
            No service on this account may submit while this is set. It is cleared only by a
            full-universe reconciliation that resolves it - never by waiting, and never by
            retrying.
          </p>
        )}
      </div>

      <div className="grid grid-cols-2 gap-x-5 gap-y-3.5 border-t border-line px-4 py-3.5 sm:grid-cols-4">
        <Field label="Set by">{panel.source ?? "—"}</Field>
        <Field label="Updated">
          <span className="num">
            {stampUtc(panel.updated_at, generatedAt)}
            {panel.updated_at ? (
              <span className="ml-1.5 text-[11px] text-ink-3">
                {relative(panel.updated_at, generatedAt)}
              </span>
            ) : null}
          </span>
        </Field>
        {budget.map((row) => (
          <BudgetLine key={row.key} row={row} />
        ))}
      </div>

      <div className="border-t border-line px-4 py-3">
        <div className="flex items-baseline justify-between gap-3">
          <h3 className="eyebrow text-ink-3">Last failure event</h3>
          {lastFailureAt ? (
            <span className="num text-[11px] text-ink-3">
              {stampUtc(lastFailureAt, generatedAt)} UTC · {relative(lastFailureAt, generatedAt)}
            </span>
          ) : null}
        </div>
        <p className="mt-1.5 text-[11.5px] leading-snug text-ink-2">
          {lastFailure ?? <span className="text-ink-3">No failure event is recorded.</span>}
        </p>
      </div>
    </Card>
  );
}
