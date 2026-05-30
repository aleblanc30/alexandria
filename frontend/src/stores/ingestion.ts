import { defineStore } from 'pinia'
import { ref, shallowRef } from 'vue'
import * as api from '@/api/client'
import { useToastStore } from './toast'

function notifyError(e: any) {
  useToastStore().push(e?.message ?? String(e), 'error')
}

const POLL_MS = 500
const SOURCES = ['firefox', 'zotero', 'calibre', 'image'] as const
type JobKind = 'metadata' | 'ingest'

function formatJobToast(src: string, p: api.SyncProgress): string {
  const job = p.active_job === 'metadata' ? 'metadata sync' : 'ingest'
  const r = p.last_result as Record<string, any> | null | undefined
  if (!r || src !== 'firefox') return `${src} ${job} complete`
  const f = r.fetch ?? {}
  const e = r.embed ?? {}
  return `${src} ingest: ${f.fetched ?? 0} fetched, ${f.unfetchable ?? 0} unfetchable, ${f.skipped ?? 0} skipped, ${e.processed ?? 0} embedded`
}

export const useIngestionStore = defineStore('ingestion', () => {
  const status          = ref<api.IngestionStatus | null>(null)
  const unfetchable     = shallowRef<api.UnfetchableRow[]>([])
  const progress        = ref<Record<string, api.SyncProgress>>({})
  /** Queued locally until the backend reports status=running (avoids poll stopping early). */
  const pendingJob      = ref<Record<string, JobKind>>({})
  let pollTimer: ReturnType<typeof setInterval> | null = null
  const notified        = new Set<string>()

  function isMetadataRunning(src: string): boolean {
    const p = progress.value[src]
    if (p?.status === 'running' && p.active_job === 'metadata') return true
    return pendingJob.value[src] === 'metadata'
  }

  function isIngestRunning(src: string): boolean {
    const p = progress.value[src]
    if (p?.status === 'running' && p.active_job === 'ingest') return true
    return pendingJob.value[src] === 'ingest'
  }

  function isAnyJobRunning(): boolean {
    return SOURCES.some(src => isMetadataRunning(src) || isIngestRunning(src))
  }

  function markPending(source: string, job: JobKind) {
    pendingJob.value = { ...pendingJob.value, [source]: job }
  }

  function clearPending(source: string) {
    if (!(source in pendingJob.value)) return
    const next = { ...pendingJob.value }
    delete next[source]
    pendingJob.value = next
  }

  function applyJobFlags(src: string, p: api.SyncProgress) {
    const pending = pendingJob.value[src]
    const running = p.status === 'running'
    const metaActive = (running && p.active_job === 'metadata') || pending === 'metadata'
    const ingestActive = (running && p.active_job === 'ingest') || pending === 'ingest'
    return { metaActive, ingestActive }
  }

  function syncPolling(snap: Record<string, api.SyncProgress>) {
    const anyRunning = Object.values(snap).some(p => p.status === 'running')
    const hasPending = Object.keys(pendingJob.value).length > 0
    if (anyRunning || hasPending) startPolling()
    else stopPolling()
  }

  function applyProgressSnapshot(snap: Record<string, api.SyncProgress>) {
    progress.value = snap
    for (const [src, p] of Object.entries(snap)) {
      if (p.status === 'running' || p.status === 'done' || p.status === 'cancelled'
          || p.status === 'error' || p.status === 'paused') {
        clearPending(src)
      }
    }
    syncPolling(snap)
  }

  async function load() {
    try {
      status.value = await api.ingestionStatus()
    } catch (e) { notifyError(e) }
    try {
      unfetchable.value = await api.unfetchableUrls()
    } catch { /* unfetchable list is non-critical */ }
    try {
      const snap = await api.syncProgress()
      applyProgressSnapshot(snap)
    } catch { /* progress hydrate is non-critical */ }
  }

  async function pollProgress() {
    try {
      const snap = await api.syncProgress()
      applyProgressSnapshot(snap)
      for (const [src, p] of Object.entries(snap)) {
        const noteKey = `${src}:${p.active_job ?? 'any'}:${p.status}`
        if (p.status === 'done' && !notified.has(noteKey)) {
          notified.add(noteKey)
          useToastStore().push(formatJobToast(src, p), 'info')
          await load()
        } else if (p.status === 'cancelled' && !notified.has(noteKey)) {
          notified.add(noteKey)
          useToastStore().push(
            p.last_result
              ? `${formatJobToast(src, p)} — stopped early, partial results saved`
              : `${src} stopped (${p.overall_processed} items processed)`,
            'info',
          )
          await load()
        } else if (p.status === 'error' && !notified.has(noteKey)) {
          notified.add(noteKey)
          useToastStore().push(`${src} failed: ${p.error ?? 'unknown error'}`, 'error', 8000)
          await load()
        }
      }
    } catch { /* ignore transient poll errors */ }
  }

  function startPolling() {
    if (pollTimer) return
    pollTimer = setInterval(pollProgress, POLL_MS)
    void pollProgress()
  }

  function stopPolling() {
    if (pollTimer) {
      clearInterval(pollTimer)
      pollTimer = null
    }
  }

  function clearNotifications(source: string, job: JobKind) {
    for (const status of ['done', 'cancelled', 'error']) {
      notified.delete(`${source}:${job}:${status}`)
    }
  }

  async function runJob(source: string, job: JobKind, fn: (s: string, f?: boolean) => Promise<unknown>) {
    clearNotifications(source, job)
    markPending(source, job)
    startPolling()
    try {
      await fn(source)
      await pollProgress()
    } catch (e: any) {
      if (e?.status === 409) {
        try {
          await fn(source, true)
          await pollProgress()
          return
        } catch (retryErr) {
          notifyError(retryErr)
        }
      } else {
        notifyError(e)
      }
      clearPending(source)
      syncPolling(progress.value)
    }
  }

  async function syncMetadata(source: string) {
    await runJob(source, 'metadata', api.syncMetadata)
  }

  async function ingest(source: string) {
    await runJob(source, 'ingest', api.syncIngest)
  }

  async function cancel(source: string) {
    try {
      await api.cancelSync(source)
      await pollProgress()
    } catch (e) { notifyError(e) }
  }

  return {
    status, unfetchable, progress, pendingJob,
    load, syncMetadata, ingest, cancel, stopPolling,
    isMetadataRunning, isIngestRunning, isAnyJobRunning,
    applyJobFlags,
  }
})
