import type { Metadata } from "next";

/**
 * A layout for one reason: the tab title.
 *
 * The page itself is a client component and cannot export `metadata`, so
 * without this file the Equity Paper page inherits "AutoTrader — Operations"
 * from the root layout. A browser tab, a bookmark or a screenshot of a page
 * showing real broker orders, labelled as something else, is the confusion
 * this whole section exists to prevent - and here it runs the other way from
 * the shadow's: a reader must not mistake the shadow for this, and must not
 * mistake this for the crypto book either.
 */
export const metadata: Metadata = {
  title: "AutoTrader — Equity Paper (Alpaca paper, no real money)",
  description:
    "Read-only record of the EDA-1 equity paper book: real paper-broker orders, fills and " +
    "positions on the shared account. No real money, and no live path.",
  robots: { index: false, follow: false },
};

export default function EquityPaperLayout({ children }: { children: React.ReactNode }) {
  return children;
}
