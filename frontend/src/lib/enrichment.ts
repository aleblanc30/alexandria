/**
 * Presentation helpers for retrieval-enrichment provenance (DESIGN.md §3.2).
 *
 * The backend owns the human-readable `label` on each entry, so nothing here
 * re-derives it. What lives here is how much to *trust* a rung, which the label
 * alone does not convey: "Brave search" and "Open Library · ISBN" read as
 * equally authoritative until you know one is an exact identifier match and the
 * other a web snippet verified in one direction only.
 */
import type { EnrichmentResolver } from '@/api/client'

export type RungKey = EnrichmentResolver | 'unknown'

/** Confidence ramp, strongest first. Drives both the colour and the ordering. */
export const RUNG_TRUST: readonly RungKey[] = [
  'isbn',
  'search',
  'google_books',
  'brave',
  'local_model',
  'unknown',
]

const HINTS: Record<RungKey, string> = {
  isbn:         'Exact match: the ISBN read off the book resolved to this edition.',
  search:       'Verified match: title and author agreed with the catalogue record.',
  google_books: 'Verified match from a second catalogue, after Open Library missed.',
  brave:        'Web snippet, verified only one-directionally — treat as the weakest rung.',
  local_model:  'Written by the local model from the document itself; not an external claim.',
  unknown:      'Provenance unknown.',
}

/** Normalise a possibly-null resolver into a key this module can key on. */
export function rungKey(resolvedBy: string | null | undefined): RungKey {
  return (RUNG_TRUST as readonly string[]).includes(resolvedBy ?? '')
    ? (resolvedBy as RungKey)
    : 'unknown'
}

/** Why this rung is worth more or less trust — surfaced as a tooltip. */
export function rungHint(resolvedBy: string | null | undefined): string {
  return HINTS[rungKey(resolvedBy)]
}

/** Modifier class for the rung badge, matching the `.rung--*` styles. */
export function rungClass(resolvedBy: string | null | undefined): string {
  return `rung--${rungKey(resolvedBy)}`
}

/** How strong a rung is: lower is stronger. Unrecognised rungs sort last. */
export function rungRank(resolvedBy: string | null | undefined): number {
  return RUNG_TRUST.indexOf(rungKey(resolvedBy))
}
