export const INGESTION_SOURCES = ['firefox', 'zotero', 'calibre', 'image', 'youtube', 'reddit'] as const
export type IngestionSource = (typeof INGESTION_SOURCES)[number]

export const SOURCE_COLORS: Record<IngestionSource, string> = {
  firefox: '#378ADD',
  zotero: '#639922',
  calibre: '#BA7517',
  image: '#7F77DD',
  youtube: '#CC0000',
  reddit: '#FF4500',
}

export const SOURCE_LABELS: Record<IngestionSource, string> = {
  firefox: 'Firefox',
  zotero: 'Zotero',
  calibre: 'Calibre',
  image: 'Images',
  youtube: 'YouTube',
  reddit: 'Reddit',
}

export function isIngestionSource(value: string): value is IngestionSource {
  return (INGESTION_SOURCES as readonly string[]).includes(value)
}

export const SOURCE_PATH_LABELS: Record<IngestionSource, string> = {
  firefox: 'Firefox profile folder',
  zotero: 'Zotero database file (zotero.sqlite)',
  calibre: 'Calibre library folder',
  image: 'Image folders',
  youtube: 'YouTube OAuth client secret (JSON)',
  reddit: 'Configured via ALEXANDRIA_REDDIT_* credentials in .env',
}

/**
 * Credential-based sources have no filesystem path to configure (they read from
 * an authenticated API). Their panel hides the path form.
 */
export function sourceIsPathBased(source: IngestionSource): boolean {
  return source !== 'reddit'
}

/** Sources that run HTTP fetch as part of the ingest job. */
export function sourceHasFetchPhase(source: IngestionSource): boolean {
  return source === 'firefox' || source === 'reddit'
}

/** Sources that embed inline during fetch (no separate embedding progress bar). */
export function sourceSkipsEmbedPhase(source: IngestionSource): boolean {
  return source === 'firefox'
}

export function ingestJobLabel(source: IngestionSource): string {
  return sourceHasFetchPhase(source) ? 'Fetch & embed' : 'Embed'
}
