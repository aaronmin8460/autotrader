import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "AutoTrader — System (services and data)",
  description:
    "Read-only operational status: trading runtimes, shadow observers, dashboard APIs, reconciliation and data freshness.",
  robots: { index: false, follow: false },
};

export default function SystemLayout({ children }: { children: React.ReactNode }) {
  return children;
}
