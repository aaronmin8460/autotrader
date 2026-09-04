import type { Metadata } from "next";

/**
 * A layout for one reason: the tab title. The page is a client component and
 * cannot export `metadata`, so without this file it would inherit the root
 * title and a screenshot of the account's positions would be labelled
 * something else.
 */
export const metadata: Metadata = {
  title: "AutoTrader — Portfolio (paper account)",
  description:
    "Read-only view of the broker paper account's positions, weights, target-vs-actual and allocation.",
  robots: { index: false, follow: false },
};

export default function PortfolioLayout({ children }: { children: React.ReactNode }) {
  return children;
}
