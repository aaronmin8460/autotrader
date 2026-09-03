import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "AutoTrader — Orders (paper account)",
  description:
    "Read-only account-wide order stream: real paper-broker orders from the crypto and equity paper stores. No simulated row is an input.",
  robots: { index: false, follow: false },
};

export default function OrdersLayout({ children }: { children: React.ReactNode }) {
  return children;
}
