import type { IngestionStatus, SyncProgress } from '@/api/client'
import type { IngestionSource } from '@/constants/sources'

export function formatJobToast(src: string, p: SyncProgress): string {
  const job = p.active_job === 'metadata' ? 'metadata sync' : 'ingest'
  const r = p.last_result as Record<string, Record<string, number>> | null | undefined
  if (!r) return `${src} ${job} complete`

  if (src === 'firefox' && p.active_job === 'ingest') {
    const f = r.fetch ?? {}
    const e = r.embed ?? {}
    return `${src} ingest: ${f.fetched ?? 0} fetched, ${f.unfetchable ?? 0} unfetchable, ${f.skipped ?? 0} skipped, ${e.processed ?? 0} embedded`
  }
  if (src === 'zotero' && p.active_job === 'ingest') {
    const e = r.embed ?? {}
    return `${src} ingest: ${e.processed ?? 0} processed, ${e.skipped ?? 0} skipped, ${e.failed ?? 0} failed`
  }
  if (src === 'calibre' && p.active_job === 'ingest') {
    const m = r.metadata_embed ?? {}
    const f = r.fulltext ?? {}
    return `${src} ingest: ${m.processed ?? 0} metadata embedded, ${f.processed ?? 0} fulltext`
  }
  if (src === 'image' && p.active_job === 'ingest') {
    const i = r.ingest ?? {}
    return `${src} ingest: ${i.processed ?? 0} processed, ${i.skipped ?? 0} skipped, ${i.failed ?? 0} failed`
  }
  if (r.metadata) {
    const m = r.metadata
    return `${src} metadata: ${m.processed ?? 0} processed, ${m.skipped ?? 0} skipped, ${m.failed ?? 0} failed`
  }
  return `${src} ${job} complete`
}

/** One-line ingest summary for a source panel (mirrors Firefox fetch summary). */
export function ingestStatsSummary(
  source: IngestionSource,
  st: IngestionStatus | null,
): string | null {
  if (!st) return null

  const stats = st.fetch_by_source?.[source]
  const pendingMeta = st.pending_metadata_by_source?.[source] ?? 0

  if (source === 'firefox') {
    if (!stats) return null
    const parts = [
      `${stats.fetched ?? 0} fetched`,
      `${stats.unfetchable ?? 0} unfetchable`,
      `${stats.skipped ?? 0} skipped`,
      `${stats.embedded ?? 0} embedded`,
    ]
    if ((stats.pending ?? 0) > 0) parts.push(`${stats.pending} pending`)
    return parts.join(' · ')
  }

  if (source === 'zotero' || source === 'calibre') {
    if (!stats && pendingMeta <= 0) return null
    const parts: string[] = []
    if (pendingMeta > 0) parts.push(`${pendingMeta} metadata pending`)
    if (stats) {
      parts.push(`${stats.available ?? 0} available`)
      if (source === 'calibre') {
        parts.push(`${stats.missing ?? 0} missing`)
      } else if ((stats.pending ?? 0) > 0) {
        parts.push(`${stats.pending ?? 0} no PDF`)
      }
      parts.push(`${stats.embedded ?? 0} embedded`)
    }
    return parts.length ? parts.join(' · ') : null
  }

  if (source === 'image') {
    if (!stats && pendingMeta <= 0) return null
    const parts: string[] = []
    if (stats) {
      parts.push(`${stats.registered ?? 0} registered`)
      parts.push(`${stats.embedded ?? 0} embedded`)
      if ((stats.pending ?? 0) > 0) parts.push(`${stats.pending} pending ingest`)
    }
    if (pendingMeta > 0) parts.push(`${pendingMeta} metadata pending`)
    return parts.length ? parts.join(' · ') : null
  }

  return null
}
