/**
 * The sparkline path, as arithmetic. Pure, so the test suite can run it.
 */

export function sparkPath(closes: number[], width: number, height: number): string {
  if (closes.length < 2) return "";
  const min = Math.min(...closes);
  const max = Math.max(...closes);
  const span = max - min || 1;
  const step = width / (closes.length - 1);
  const pad = 2;
  return closes
    .map((close, index) => {
      const x = index * step;
      const y = pad + (height - pad * 2) * (1 - (close - min) / span);
      return `${index === 0 ? "M" : "L"}${x.toFixed(1)} ${y.toFixed(1)}`;
    })
    .join(" ");
}
