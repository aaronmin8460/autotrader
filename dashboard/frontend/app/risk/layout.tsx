import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "AutoTrader — Risk (deployed policy)",
  description:
    "Read-only view of the deployed sizing policy's targets and hard caps against the broker's current account.",
  robots: { index: false, follow: false },
};

export default function RiskLayout({ children }: { children: React.ReactNode }) {
  return children;
}
