/**
 * The Operations header: the account's verdict, on the shared navigation.
 *
 * Kept as a thin wrapper around `Nav` so the page composition reads the same
 * as the other two sections. The links `/`, `/equity-paper` and `/shadows`
 * come from `SECTIONS`; this file adds nothing to them.
 */

import type { Overview } from "@/lib/types";

import { Nav } from "./Nav";

export function Header({
  overview,
  connected,
  lastSuccessAt,
}: {
  overview: Overview | null;
  connected: boolean;
  lastSuccessAt: string | null;
}) {
  return (
    <Nav
      section="operations"
      verdict={overview?.system_state ?? null}
      verdictTone={overview?.system_state_tone ?? "MUTED"}
      verdictTitle={overview?.attention.join(" ") || "Broker account"}
      connected={connected}
      lastSuccessAt={lastSuccessAt}
    />
  );
}
