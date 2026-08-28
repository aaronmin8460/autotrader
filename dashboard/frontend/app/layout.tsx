import type { Metadata, Viewport } from "next";

import "./globals.css";

export const metadata: Metadata = {
  title: "AutoTrader — Operations",
  description:
    "Read-only operations view of the AutoTrader Alpaca paper system. Displays state; changes none.",
  robots: { index: false, follow: false },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  themeColor: [
    { media: "(prefers-color-scheme: dark)", color: "#0a0c0d" },
    { media: "(prefers-color-scheme: light)", color: "#f4f5f6" },
  ],
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-full antialiased">{children}</body>
    </html>
  );
}
