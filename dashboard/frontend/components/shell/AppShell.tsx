"use client";

/**
 * The application frame: sidebar, status bar, content, footer.
 *
 * Every page renders inside this and nothing else. The frame owns the three
 * shared polls (see `lib/dashboard`), so navigating between eight routes does
 * not multiply the request rate by eight — the cadence is the same as the one
 * Operations alone used at Dashboard V2.
 *
 * Layout: the sidebar is a fixed-width flex column, the content is the
 * remainder with `min-w-0` so a wide table scrolls inside its own panel rather
 * than pushing the page sideways. Content is capped at 1720px and centred, so
 * a 1920 display gains air rather than line length.
 */

import type { ReactNode } from "react";

import { useDashboard } from "@/lib/dashboard";
import { useI18n } from "@/lib/i18n";

import { Sidebar } from "./Sidebar";
import { TopStatusBar } from "./TopStatusBar";

function Footer() {
  const { t } = useI18n();
  const { accountIntervalMs } = useDashboard();
  return (
    <footer className="mt-8 flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-subtle pt-4 text-meta text-ink-3">
      <span>{t("common.readOnly")}</span>
      <span>{t("common.timesUtc")}</span>
      <span className="num">{t("common.refreshEvery", { seconds: accountIntervalMs / 1000 })}</span>
    </footer>
  );
}

export function AppShell({ children }: { children: ReactNode }) {
  const { t } = useI18n();
  return (
    <div className="flex min-h-dvh">
      <a
        href="#main"
        className="sr-only rounded-sm bg-surface-3 px-3 py-2 text-body text-ink focus:not-sr-only focus:absolute focus:top-2 focus:left-2 focus:z-(--z-tooltip)"
      >
        {t("app.skipToContent")}
      </a>

      <Sidebar />

      <div className="flex min-w-0 flex-1 flex-col">
        <TopStatusBar />
        <main id="main" className="mx-auto w-full max-w-[1720px] flex-1 px-5 py-5 sm:px-6">
          {children}
          <Footer />
        </main>
      </div>
    </div>
  );
}
