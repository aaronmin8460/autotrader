"use client";

/**
 * The top of the Live page. Four facts, before anything else is read.
 *
 *   LIVE · REAL MONEY        which account this is
 *   LIVE READY / NOT READY   whether the preconditions are met
 *   ARMED / DISARMED         whether an order can actually leave
 *   the mutation sentence    what that means, in words
 *
 * The wording is deliberately never `ACTIVE` on its own. "Active" answers
 * neither of the two questions an operator has, and a page that said it would
 * be ambiguous in exactly the direction that costs money.
 *
 * **DISARMED is not styled as an error.** It is the correct state for this
 * entire program and is drawn neutral, with a line saying so. The states that
 * draw the eye are ARMED — where a real-money order can leave the building —
 * and UNKNOWN, because an arm switch nobody can read has not been proven off.
 *
 * There is no control here. No arm button, no disarm button, no start, no stop,
 * no withdraw. This page renders state; every one of those lives outside it,
 * and the read-only notice says so rather than leaving an operator hunting for
 * a button that was deliberately not built.
 */

import { useT } from "@/lib/i18n";
import type { ReadinessView } from "@/lib/live";
import { stampUtc } from "@/lib/format";
import { cn, Surface } from "../ui";
import { Note, Statement } from "./atoms";

export function LiveHeader({
  view,
  changedAt,
  generatedAt,
}: {
  view: ReadinessView;
  changedAt: string | null;
  generatedAt: string | null;
}) {
  const t = useT();
  const armed = view.arm === "ARMED";

  return (
    <Surface
      label={t("live.title")}
      className={cn(
        "px-4 py-4 sm:px-5",
        // The tint is redundancy for the words below it, never the message.
        armed ? "tint-neg" : "tint-warn",
      )}
    >
      <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
        <div className="min-w-0">
          <span className="text-eyebrow font-semibold tracking-[0.14em] text-warn uppercase">
            {t("env.live.realMoney")}
          </span>

          {/* The two states, at the largest size on the page, side by side. */}
          <div className="mt-3 flex flex-wrap items-center gap-x-5 gap-y-2">
            <span
              className={cn(
                "text-value font-semibold tracking-[-0.01em]",
                view.readyTone === "POSITIVE"
                  ? "text-pos"
                  : view.readyTone === "ATTENTION"
                    ? "text-warn"
                    : "text-ink-3",
              )}
            >
              {t(view.readyKey)}
            </span>
            <span aria-hidden className="h-5 w-px bg-subtle" />
            <span
              className={cn(
                "text-value font-semibold tracking-[-0.01em]",
                armed ? "text-neg" : view.arm === "DISARMED" ? "text-ink" : "text-warn",
              )}
            >
              {t(view.armKey)}
            </span>
          </div>

          {/* What the arm state means, spelled out. Always present. */}
          <Statement tone={view.mutationTone} emphasis className="mt-3">
            {t(view.mutationKey)}
          </Statement>

          {view.arm === "DISARMED" ? (
            <Note className="mt-2">{t("live.arm.disarmedIsSafe")}</Note>
          ) : null}
          {view.armReason ? <Note className="mt-2">{view.armReason}</Note> : null}
          {/*
            Prose, not a Tag. `Tag` is `whitespace-nowrap` - correct for PAPER,
            FILLED and a policy id, and wrong for a sentence, which at 675px
            wide pushed the whole page into horizontal scroll on any viewport
            narrower than a laptop.
          */}
          <Note className="mt-3">{t("live.readOnlyNotice")}</Note>
        </div>

        <div className="flex shrink-0 flex-col items-start gap-1 sm:items-end">
          {changedAt ? (
            <span className="text-meta text-ink-3">
              {t("live.arm.changedAt")}{" "}
              <span className="num">{stampUtc(changedAt, generatedAt)}</span>
            </span>
          ) : null}
          <span className="text-meta text-ink-3">
            <span className="num">{stampUtc(generatedAt, generatedAt)}</span>
          </span>
        </div>
      </div>
    </Surface>
  );
}
