"use client";

/**
 * The left navigation: a LEVEL 2 floating rail, collapsible to icons.
 *
 * Eight destinations, two of them nested under Strategies. The nesting is the
 * information architecture, not decoration: Equity Paper and Shadows are two
 * views of the same question — what is deployed, and what only watches — and
 * a strategy that grew a top-level tab of its own is how a navigation stops
 * being a navigation.
 *
 * Shadows carries the violet accent wherever it appears, including here, so
 * the observation boundary is visible before the page loads.
 *
 * Collapsed, every item keeps its accessible name and its tooltip; the icon is
 * never the only label.
 */

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useSyncExternalStore } from "react";

import { useI18n } from "@/lib/i18n";
import { navCollapsedPref } from "@/lib/prefs";

import { cn } from "../ui";
import {
  CollapseIcon,
  OrdersIcon,
  OverviewIcon,
  PaperIcon,
  PortfolioIcon,
  RiskIcon,
  ShadowIcon,
  StrategiesIcon,
  SystemIcon,
} from "./icons";
import { NAV_ITEMS, sectionForPath, type SectionKey } from "./nav-model";

const ICONS: Record<SectionKey, () => React.JSX.Element> = {
  overview: OverviewIcon,
  portfolio: PortfolioIcon,
  strategies: StrategiesIcon,
  equityPaper: PaperIcon,
  shadows: ShadowIcon,
  orders: OrdersIcon,
  risk: RiskIcon,
  system: SystemIcon,
};

export function useNavCollapsed(): [boolean, (next: boolean) => void] {
  const collapsed = useSyncExternalStore(
    navCollapsedPref.subscribe,
    navCollapsedPref.read,
    navCollapsedPref.server,
  );
  return [collapsed, navCollapsedPref.write];
}

export function Sidebar() {
  const { t } = useI18n();
  const pathname = usePathname() ?? "/";
  const active = sectionForPath(pathname);
  const [collapsed, setCollapsed] = useNavCollapsed();

  return (
    <div
      className={cn(
        "floating sticky top-0 flex h-dvh flex-col border-r",
        "z-(--z-sidebar) transition-[width] duration-(--duration-fast) ease-(--ease-out)",
        collapsed ? "w-[60px]" : "w-[212px]",
      )}
    >
      <div className={cn("flex h-14 shrink-0 items-center gap-2", collapsed ? "justify-center px-2" : "px-4")}>
        <span aria-hidden className="size-2 shrink-0 rounded-[1px] bg-accent" />
        {collapsed ? null : (
          <span className="text-body font-semibold tracking-tight text-ink">{t("app.name")}</span>
        )}
      </div>

      <nav aria-label={t("nav.label")} className="min-h-0 flex-1 overflow-y-auto px-2 pb-3">
        <ul className="space-y-0.5">
          {NAV_ITEMS.map((item) => {
            const Icon = ICONS[item.key];
            const isActive = item.key === active;
            const label = t(item.labelKey);
            return (
              <li key={item.key}>
                <Link
                  href={item.href}
                  aria-current={isActive ? "page" : undefined}
                  title={collapsed ? `${label} — ${t(item.detailKey)}` : t(item.detailKey)}
                  className={cn(
                    "group flex items-center gap-2.5 rounded-sm py-1.5",
                    "transition-colors duration-(--duration-instant) focus-visible:outline-2 focus-visible:outline-accent",
                    collapsed ? "justify-center px-2" : "px-2.5",
                    !collapsed && item.child && "ms-3",
                    isActive
                      ? item.observe
                        ? "bg-surface-3 text-observe"
                        : "bg-surface-3 text-ink"
                      : "text-ink-3 hover:bg-surface-2 hover:text-ink-2",
                  )}
                >
                  <span className={cn(item.observe && !isActive && "text-observe/70")}>
                    <Icon />
                  </span>
                  {collapsed ? (
                    <span className="sr-only">{label}</span>
                  ) : (
                    <span className="truncate text-table font-medium">{label}</span>
                  )}
                  {isActive && !collapsed ? (
                    <span
                      aria-hidden
                      className={cn(
                        "ms-auto h-3.5 w-[2px] rounded-full",
                        item.observe ? "bg-observe" : "bg-accent",
                      )}
                    />
                  ) : null}
                </Link>
              </li>
            );
          })}
        </ul>
      </nav>

      <div className={cn("shrink-0 border-t border-subtle p-2", collapsed && "flex justify-center")}>
        <button
          type="button"
          onClick={() => setCollapsed(!collapsed)}
          aria-expanded={!collapsed}
          aria-label={collapsed ? t("nav.expand") : t("nav.collapse")}
          title={collapsed ? t("nav.expand") : t("nav.collapse")}
          className={cn(
            "flex items-center gap-2 rounded-sm px-2 py-1.5 text-ink-3",
            "hover:bg-surface-2 hover:text-ink-2 focus-visible:outline-2 focus-visible:outline-accent",
            collapsed ? "justify-center" : "w-full",
          )}
        >
          <CollapseIcon collapsed={collapsed} />
          {collapsed ? null : <span className="text-meta">{t("nav.collapse")}</span>}
        </button>
      </div>
    </div>
  );
}
