/** Canonical cluster colour palette, shared by ClusterView, TrendsView and ScatterPlot. */
export const PALETTE = [
  '#378ADD', '#7F77DD', '#639922', '#BA7517',
  '#1D9E75', '#D85A30', '#D4537E', '#888780',
]

/** Stable colour for a 0-based index (wraps around the palette). */
export function colorForIndex(i: number): string {
  return PALETTE[i % PALETTE.length]
}
