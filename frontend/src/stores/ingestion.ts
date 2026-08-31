import { defineStore } from 'pinia'
import { ref, shallowRef } from 'vue'
import * as api from '@/api/client'
import { usePolling } from '@/composables/usePolling'
import { formatJobToast } from '@/lib/ingestStats'
import { notifyError } from '@/lib/notifyError'
import { useToastStore } from './toast'

/** Fallback only: the live path is one SSE stream per running source. */
const POLL_MS = 500
const SOURCES = ['firefox', 'zotero', 'calibre', 'image'] as const
type JobKind = 'metadata' | 'ingest'
type Source = typeof SOURCES[number]

export const useIngestionStore = defineStore('ingestion', () => {
  const status          = ref<api.IngestionStatus | null>(null)
  const unfetchable     = shallowRef<api.UnfetchableRow[]>([])
  const domains         = shallowRef<api.DomainTopLists | null>(null)
  const progress        = ref<Record<string, api.SyncProgress>>({})
  /** Queued locally until the backend reports status=running (avoids poll stopping early). */
  const pendingJob      = ref<Record<string, JobKind>>({})
  let progressHydrated  = false
  const notified        = new Set<string>()
  /** One open stream per running source (see GET /ingestion/sync/events). */
  const streams         = new Map<string, EventSource>()
  /** Set when EventSource fails; the store falls back to interval polling. */
  let streamsBroken     = false

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

  function activeSources(): Source[] {
    return SOURCES.filter(src => {
      if (src in pendingJob.value) return true
      return progress.value[src]?.status === 'running'
    })
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

  /** Watch exactly the sources with a job on them, and nothing else. */
  function syncStreams() {
    const active = new Set<string>(activeSources())
    if (streamsBroken) {
      if (active.size > 0) { startPolling(); return }
      // Nothing running: drop back to streaming and let the next job try again.
      stopPolling()
      streamsBroken = false
      return
    }
    for (const src of streams.keys()) {
      if (!active.has(src)) closeStream(src)
    }
    for (const src of active) openStream(src)
  }

  function openStream(src: string) {
    if (streams.has(src)) return
    streams.set(src, api.syncEvents(
      src,
      ev => { void handleEvent(src, ev) },
      () => {
        // The server closes the stream itself once a job ends, which surfaces
        // here as an error; only treat it as a transport failure while the job
        // is still supposed to be running.
        closeStream(src)
        if (activeSources().includes(src as Source)) {
          streamsBroken = true
          syncStreams()
        }
      },
    ))
  }

  function closeStream(src: string) {
    streams.get(src)?.close()
    streams.delete(src)
  }

  function stopStreams() {
    for (const src of [...streams.keys()]) closeStream(src)
    stopPolling()
  }

  /** Fold the per-source slice the event carries into the cached status object. */
  function applyCounts(src: string, counts: api.SourceCounts) {
    const st = status.value
    if (!st) return
    status.value = {
      ...st,
      pending_metadata_by_source: { ...st.pending_metadata_by_source, [src]: counts.pending_metadata },
      fetch_by_source: { ...st.fetch_by_source, [src]: counts.fetch },
      source_unavailable: { ...st.source_unavailable, [src]: counts.unavailable },
    }
  }

  async function handleEvent(src: string, ev: api.SyncEvent) {
    applyCounts(src, ev.counts)
    applyProgressSnapshot({ [src]: ev.progress }, { merge: true })
    await notifyTerminal(src, ev.progress)
  }

  function applyProgressSnapshot(
    snap: Record<string, api.SyncProgress>,
    { merge = false }: { merge?: boolean } = {},
  ) {
    progress.value = merge ? { ...progress.value, ...snap } : snap
    for (const [src, p] of Object.entries(snap)) {
      if (p.status === 'running' || p.status === 'done' || p.status === 'cancelled'
          || p.status === 'error' || p.status === 'paused') {
        clearPending(src)
      }
    }
    syncStreams()
  }

  async function fetchProgressForActiveSources(): Promise<Record<string, api.SyncProgress> | null> {
    const targets = activeSources()
    if (targets.length === 0) return null

    const merged: Record<string, api.SyncProgress> = {}
    for (const src of targets) {
      const snap = await api.syncProgress(src)
      Object.assign(merged, snap)
    }
    return merged
  }

  async function load(force = false) {
    const jobsActive = activeSources().length > 0
    const needProgress = force || jobsActive || !progressHydrated

    try {
      status.value = await api.ingestionStatus()
    } catch (e) { notifyError(e) }

    if (needProgress) {
      try {
        const snap = jobsActive
          ? await fetchProgressForActiveSources()
          : await api.syncProgress()
        if (snap) {
          applyProgressSnapshot(snap, { merge: jobsActive })
          progressHydrated = true
        }
      } catch { /* progress hydrate is non-critical */ }
    }

    try {
      unfetchable.value = await api.unfetchableUrls()
    } catch { /* unfetchable list is non-critical */ }

    try {
      domains.value = await api.domainTopLists()
    } catch { /* domain top lists are non-critical */ }
  }

  async function pollProgress() {
    if (activeSources().length === 0) {
      stopPolling()
      return
    }

    try {
      const snap = await fetchProgressForActiveSources()
      if (!snap) {
        stopPolling()
        return
      }

      applyProgressSnapshot(snap, { merge: true })

      for (const [src, p] of Object.entries(snap)) {
        await notifyTerminal(src, p)
      }
    } catch { /* ignore transient poll errors */ }
  }

  /** Toast a job that just reached a terminal state — once per job, per source. */
  async function notifyTerminal(src: string, p: api.SyncProgress) {
    const noteKey = `${src}:${p.active_job ?? 'any'}:${p.status}`
    if (notified.has(noteKey)) return
    if (p.status === 'done') {
      notified.add(noteKey)
      useToastStore().push(formatJobToast(src, p), 'info')
    } else if (p.status === 'cancelled') {
      notified.add(noteKey)
      useToastStore().push(
        p.last_result
          ? `${formatJobToast(src, p)} — stopped early, partial results saved`
          : `${src} stopped (${p.overall_processed} items processed)`,
        'info',
      )
    } else if (p.status === 'error') {
      notified.add(noteKey)
      useToastStore().push(`${src} failed: ${p.error ?? 'unknown error'}`, 'error', 8000)
    } else {
      return
    }
    await load(true)
  }

  const { start: startPolling, stop: stopPolling } = usePolling(pollProgress, POLL_MS)

  function clearNotifications(source: string, job: JobKind) {
    for (const status of ['done', 'cancelled', 'error']) {
      notified.delete(`${source}:${job}:${status}`)
    }
  }

  async function runJob(source: string, job: JobKind, fn: (s: string, f?: boolean) => Promise<unknown>) {
    clearNotifications(source, job)
    markPending(source, job)
    syncStreams()
    try {
      await fn(source)
      await pollProgress()
    } catch (e: any) {
      if (e?.status === 409) {
        // A job is already running server-side (force=true would cancel it).
        // Surface the conflict instead of silently restarting the sync.
        useToastStore().push(
          `${source}: a sync is already running — cancel it first to restart`,
          'info',
        )
        await pollProgress()
      } else {
        notifyError(e)
      }
      clearPending(source)
      syncStreams()
    }
  }

  async function syncMetadata(source: string) {
    await runJob(source, 'metadata', api.syncMetadata)
  }

  /** Same job as syncMetadata, but re-walks the source's whole history. */
  async function backfillMetadata(source: string) {
    await runJob(source, 'metadata', api.backfillMetadata)
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

  async function purge(source: string): Promise<boolean> {
    try {
      const res = await api.purgeSource(source)
      const docs = res.counts.documents ?? res.counts.images ?? 0
      useToastStore().push(`${source}: purged ${docs} document(s)`, 'info')
      await load(true)
      return true
    } catch (e) {
      notifyError(e)
      return false
    }
  }

  return {
    status, unfetchable, domains, progress, pendingJob,
    load, syncMetadata, backfillMetadata, ingest, cancel, purge, stopStreams,
    isMetadataRunning, isIngestRunning, isAnyJobRunning,
    applyJobFlags,
  }
})
