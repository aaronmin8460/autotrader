/**
 * The exposure rail's axis.
 *
 * Split out of the component so the test suite can run it directly, and
 * because it is a decision about how a limit is presented rather than about
 * how a div is drawn.
 *
 * The rail is scaled to the limit, not to 100% of equity. An 11% per-symbol
 * cap drawn on a 0-100% axis puts 89% of the track in the past-the-cap colour
 * and squeezes the three figures that matter into its first tenth, so a
 * healthy 9% position renders as a mostly-red bar. That is the "a big number
 * is not a breach" mistake, drawn.
 */

/**
 * The top of the axis: enough headroom past the hard cap that the breach
 * region reads as a region rather than an edge, and never past 100% of equity,
 * where the quantity stops meaning anything on this dashboard.
 */
export function railDomain(
  current: number | null,
  target: number | null,
  cap: number | null,
): number {
  const candidates = [
    cap === null ? 0 : cap * 1.3,
    target === null ? 0 : target * 1.6,
    current === null ? 0 : current * 1.15,
    0.05,
  ];
  return Math.min(1, Math.max(...candidates));
}
