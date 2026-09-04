/**
 * Navigation icons: inline SVG, 16px, 1.5px stroke, `currentColor`.
 *
 * Inline rather than an icon package for two reasons. The deployed CSP is
 * `img-src 'self'` with no `data:`, so an icon that arrived as an image or a
 * data URI would not render at all; and an icon font would need `font-src`
 * widened for decoration. These are three hundred bytes of markup that inherit
 * the text colour and therefore both palettes.
 *
 * They are decoration beside a word, never the label: every navigation item
 * prints its name, and the icon rail keeps the name as an accessible label and
 * a tooltip.
 */

import type { ReactNode } from "react";

function Glyph({ children }: { children: ReactNode }) {
  return (
    <svg
      viewBox="0 0 16 16"
      className="size-4 shrink-0"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.5}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
      focusable="false"
    >
      {children}
    </svg>
  );
}

export function OverviewIcon() {
  return (
    <Glyph>
      <rect x="2" y="2" width="5" height="5" rx="1" />
      <rect x="9" y="2" width="5" height="5" rx="1" />
      <rect x="2" y="9" width="5" height="5" rx="1" />
      <rect x="9" y="9" width="5" height="5" rx="1" />
    </Glyph>
  );
}

export function PortfolioIcon() {
  return (
    <Glyph>
      <path d="M2 13h12" />
      <rect x="3" y="8" width="2.5" height="5" rx="0.5" />
      <rect x="6.75" y="5" width="2.5" height="8" rx="0.5" />
      <rect x="10.5" y="2.5" width="2.5" height="10.5" rx="0.5" />
    </Glyph>
  );
}

export function StrategiesIcon() {
  return (
    <Glyph>
      <circle cx="4" cy="4" r="2" />
      <circle cx="12" cy="12" r="2" />
      <path d="M4 6v3a3 3 0 0 0 3 3h3" />
    </Glyph>
  );
}

export function OrdersIcon() {
  return (
    <Glyph>
      <path d="M3 3h10" />
      <path d="M3 8h10" />
      <path d="M3 13h6" />
    </Glyph>
  );
}

export function RiskIcon() {
  return (
    <Glyph>
      <path d="M8 2 2.5 4.5V8c0 3 2.3 5.3 5.5 6 3.2-.7 5.5-3 5.5-6V4.5z" />
      <path d="M8 6v3" />
      <path d="M8 11h.01" />
    </Glyph>
  );
}

export function SystemIcon() {
  return (
    <Glyph>
      <rect x="2" y="3" width="12" height="4" rx="1" />
      <rect x="2" y="9" width="12" height="4" rx="1" />
      <path d="M4.5 5h.01M4.5 11h.01" />
    </Glyph>
  );
}

export function PaperIcon() {
  return (
    <Glyph>
      <path d="M4 2h5l3 3v9H4z" />
      <path d="M9 2v3h3" />
    </Glyph>
  );
}

export function ShadowIcon() {
  return (
    <Glyph>
      <circle cx="8" cy="8" r="5.5" />
      <path d="M8 2.5a5.5 5.5 0 0 0 0 11z" fill="currentColor" stroke="none" />
    </Glyph>
  );
}

export function CollapseIcon({ collapsed }: { collapsed: boolean }) {
  return (
    <Glyph>
      <rect x="2" y="2.5" width="12" height="11" rx="1.5" />
      <path d="M6.5 2.5v11" />
      {collapsed ? <path d="M9 6.5 11 8l-2 1.5" /> : <path d="M11.5 6.5 9.5 8l2 1.5" />}
    </Glyph>
  );
}
