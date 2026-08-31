import type { Metadata } from "next";

/**
 * A layout for one reason: the tab title.
 *
 * The page itself is a client component and cannot export `metadata`, so
 * without this file the Equity Shadow page inherits "AutoTrader — Operations"
 * from the root layout - and a browser tab, a bookmark, or a screenshot of the
 * shadow record labelled *Operations* is exactly the confusion this whole
 * section is built to prevent. The title says what the record is before the
 * page has rendered a pixel.
 */
export const metadata: Metadata = {
  title: "AutoTrader — Equity Shadow (observation only)",
  description:
    "Read-only observation record of the V3 + EDA-1 equity shadow. Decisions are recorded, " +
    "never taken: zero order mutation.",
  robots: { index: false, follow: false },
};

export default function EquityShadowLayout({ children }: { children: React.ReactNode }) {
  return children;
}
