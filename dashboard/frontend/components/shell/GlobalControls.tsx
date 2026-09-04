"use client";

/**
 * The two global controls: language and theme.
 *
 * Both are display preferences. Neither touches trading state, neither is sent
 * anywhere, and neither changes a number — switching to Korean re-labels the
 * interface and leaves `$101,995.05` as `$101,995.05`, because the account's
 * currency is a fact about the account and not about the reader.
 *
 * Language switching preserves the route, the open drawer and the selected
 * symbol, because it changes React state and never navigates.
 */

import { useSyncExternalStore } from "react";

import { useI18n } from "@/lib/i18n";
import { LOCALES, type Locale } from "@/lib/i18n";
import { readTheme, serverTheme, subscribeTheme, writeTheme, type Theme } from "@/lib/i18n/theme";

import { cn } from "../ui";

const LOCALE_LABEL: Record<Locale, string> = { en: "EN", ko: "한국어" };

function Segment({
  active,
  onClick,
  label,
  title,
  children,
}: {
  active: boolean;
  onClick: () => void;
  label: string;
  title?: string;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      aria-label={label}
      title={title ?? label}
      className={cn(
        "px-2 py-1 text-meta leading-none font-medium tracking-[0.04em]",
        "first:rounded-l-sm last:rounded-r-sm transition-colors duration-(--duration-instant)",
        "focus-visible:outline-2 focus-visible:outline-accent",
        active ? "bg-surface-3 text-ink" : "text-ink-3 hover:text-ink-2",
      )}
    >
      {children}
    </button>
  );
}

export function LanguageSwitcher() {
  const { locale, setLocale, t } = useI18n();
  return (
    <div role="group" aria-label={t("control.language")} className="inline-flex rounded-sm ring-1 ring-subtle">
      {LOCALES.map((option) => (
        <Segment
          key={option}
          active={option === locale}
          onClick={() => setLocale(option)}
          label={t(option === "en" ? "control.language.en" : "control.language.ko")}
        >
          {LOCALE_LABEL[option]}
        </Segment>
      ))}
    </div>
  );
}

const THEMES_ORDER: ReadonlyArray<Theme> = ["dark", "light"];

function ThemeGlyph({ theme }: { theme: Theme }) {
  return (
    <svg
      viewBox="0 0 16 16"
      className="size-3.5"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.5}
      strokeLinecap="round"
      aria-hidden
      focusable="false"
    >
      {theme === "dark" ? (
        <path d="M13 9.5A5.5 5.5 0 0 1 6.5 3a5.5 5.5 0 1 0 6.5 6.5z" />
      ) : (
        <>
          <circle cx="8" cy="8" r="3" />
          <path d="M8 1v1.5M8 13.5V15M1 8h1.5M13.5 8H15M3.2 3.2l1 1M11.8 11.8l1 1M12.8 3.2l-1 1M4.2 11.8l-1 1" />
        </>
      )}
    </svg>
  );
}

export function ThemeSwitcher() {
  const { t } = useI18n();
  const theme = useSyncExternalStore(subscribeTheme, readTheme, serverTheme);
  return (
    <div role="group" aria-label={t("control.theme")} className="inline-flex rounded-sm ring-1 ring-subtle">
      {THEMES_ORDER.map((option) => (
        <Segment
          key={option}
          active={option === theme}
          onClick={() => writeTheme(option)}
          label={t(option === "dark" ? "control.theme.dark" : "control.theme.light")}
        >
          <ThemeGlyph theme={option} />
        </Segment>
      ))}
    </div>
  );
}
