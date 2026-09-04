import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "AutoTrader — Strategies (deployed and observing)",
  description:
    "Read-only strategy lifecycle: which strategies trade the paper account, which only record decisions, and which are intentionally off.",
  robots: { index: false, follow: false },
};

export default function StrategiesLayout({ children }: { children: React.ReactNode }) {
  return children;
}
