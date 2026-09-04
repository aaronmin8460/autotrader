import type { Metadata, Viewport } from "next";

import { AppShell } from "@/components/shell/AppShell";
import { DashboardProvider } from "@/lib/dashboard";
import { I18nProvider } from "@/lib/i18n";
import { BOOTSTRAP_SCRIPT } from "@/lib/i18n/locale";

import "./globals.css";

export const metadata: Metadata = {
  title: "AutoTrader — Operations",
  description:
    "Read-only operations view of the AutoTrader paper trading system. Displays state; changes none.",
  robots: { index: false, follow: false },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  themeColor: "#080b0e",
};

/**
 * The application frame.
 *
 * Two things happen before the first paint, both in one inline script:
 * `data-theme` is set from the stored preference so the dark palette never
 * flashes, and `lang` is set from the stored locale so assistive technology
 * reads the right language from the first frame. The deployed CSP already
 * allows inline script — the Next.js App Router bootstrap requires it — so
 * this widens nothing. It writes two attributes and reads two keys of
 * `localStorage`; it sends nothing anywhere and cannot fail the page (the
 * whole body is wrapped in try/catch and falls back to the dark default).
 *
 * `lang="en"` is on the served markup because that is the locale the server
 * renders; the script and the locale provider agree on the browser's own
 * answer immediately after.
 */
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" data-theme="dark" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: BOOTSTRAP_SCRIPT }} />
      </head>
      <body className="min-h-full antialiased">
        <I18nProvider>
          <DashboardProvider>
            <AppShell>{children}</AppShell>
          </DashboardProvider>
        </I18nProvider>
      </body>
    </html>
  );
}
