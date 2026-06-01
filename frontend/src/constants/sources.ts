export const INGESTION_SOURCES = ['firefox', 'zotero', 'calibre', 'image'] as const
export type IngestionSource = (typeof INGESTION_SOURCES)[number]

export const SOURCE_COLORS: Record<IngestionSource, string> = {
  firefox: '#378ADD',
  zotero: '#639922',
  calibre: '#BA7517',
  image: '#7F77DD',
}

export const SOURCE_LABELS: Record<IngestionSource, string> = {
  firefox: 'Firefox',
  zotero: 'Zotero',
  calibre: 'Calibre',
  image: 'Images',
}

export function isIngestionSource(value: string): value is IngestionSource {
  return (INGESTION_SOURCES as readonly string[]).includes(value)
}

/** Sources that run HTTP fetch as part of the ingest job. */
export function sourceHasFetchPhase(source: IngestionSource): boolean {
  return source === 'firefox'
}

/** Sources that embed inline during fetch (no separate embedding progress bar). */
export function sourceSkipsEmbedPhase(source: IngestionSource): boolean {
  return source === 'firefox'
}

export function ingestJobLabel(source: IngestionSource): string {
  return sourceHasFetchPhase(source) ? 'Fetch & embed' : 'Embed'
}
