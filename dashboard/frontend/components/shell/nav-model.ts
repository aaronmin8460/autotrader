/**
 * The navigation model: nine destinations, one of them real money.
 *
 * **Every route that existed at Dashboard V2 keeps its exact path.** `/`,
 * `/equity-paper` and `/shadows` are the deployed, documented, bookmarked
 * addresses, and `/equity-shadow` is already a permanent redirect kept alive
 * for an older bookmark. Nothing here renames or redirects any of them; the
 * four new paths are additions, so no deep link can break in this release.
 *
 * Labels come from the translation layer by key. Paths never do.
 */

import type { MessageKey } from "@/lib/i18n";

export type SectionKey =
  | "overview"
  | "portfolio"
  | "strategies"
  | "equityPaper"
  | "live"
  | "shadows"
  | "orders"
  | "risk"
  | "system";

export interface NavItem {
  key: SectionKey;
  href: string;
  labelKey: MessageKey;
  detailKey: MessageKey;
  /** Rendered indented under `strategies`. */
  child?: boolean;
  /** Observation-only destinations carry the violet accent. */
  observe?: boolean;
  /**
   * The real-money destination.
   *
   * Exactly one item may carry this, and it is the only place in the
   * navigation where an order can reach a real broker. It is marked here
   * rather than inferred from the path so the distinction survives a rename,
   * and so `Sidebar` never has to know which string means real money.
   */
  live?: boolean;
}

export const NAV_ITEMS: ReadonlyArray<NavItem> = [
  { key: "overview", href: "/", labelKey: "nav.overview", detailKey: "nav.detail.overview" },
  { key: "portfolio", href: "/portfolio", labelKey: "nav.portfolio", detailKey: "nav.detail.portfolio" },
  { key: "strategies", href: "/strategies", labelKey: "nav.strategies", detailKey: "nav.detail.strategies" },
  {
    key: "equityPaper",
    href: "/equity-paper",
    labelKey: "nav.equityPaper",
    detailKey: "nav.detail.equityPaper",
    child: true,
  },
  {
    key: "live",
    href: "/live",
    labelKey: "nav.live",
    detailKey: "nav.detail.live",
    child: true,
    live: true,
  },
  {
    key: "shadows",
    href: "/shadows",
    labelKey: "nav.shadows",
    detailKey: "nav.detail.shadows",
    child: true,
    observe: true,
  },
  { key: "orders", href: "/orders", labelKey: "nav.orders", detailKey: "nav.detail.orders" },
  { key: "risk", href: "/risk", labelKey: "nav.risk", detailKey: "nav.detail.risk" },
  { key: "system", href: "/system", labelKey: "nav.system", detailKey: "nav.detail.system" },
];

/** The section a pathname belongs to, for `aria-current` and the page title. */
export function sectionForPath(pathname: string): SectionKey {
  if (pathname === "/") return "overview";
  const match = NAV_ITEMS.filter((item) => item.href !== "/").find((item) =>
    pathname.startsWith(item.href),
  );
  if (match) return match.key;
  // The legacy redirect target resolves to the workspace it forwards to.
  if (pathname.startsWith("/equity-shadow")) return "shadows";
  return "overview";
}
