import type { Metadata } from "next";

/**
 * A layout for one reason: the tab title.
 *
 * The page is a client component and cannot export `metadata`; without this
 * file the Shadows page inherits "AutoTrader — Operations" from the root
 * layout, and a screenshot of an observation record labelled *Operations* is
 * exactly the confusion this section exists to prevent.
 */
export const metadata: Metadata = {
  title: "AutoTrader — Shadows (observation only)",
  description:
    "Read-only observation records of the V3 + EDA-1 equity shadow and the A1-B U30 shadow. " +
    "Decisions are recorded, never taken: zero order mutation.",
  robots: { index: false, follow: false },
};

export default function ShadowsLayout({ children }: { children: React.ReactNode }) {
  return children;
}
