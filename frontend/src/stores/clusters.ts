import { defineStore } from 'pinia'
import { ref, shallowRef } from 'vue'
import * as api from '@/api/client'
import { usePolling } from '@/composables/usePolling'
import { notifyError } from '@/lib/notifyError'
import { useToastStore } from './toast'

const POLL_MS = 1500

export const useClustersStore = defineStore('clusters', () => {
  const list        = shallowRef<api.ClusterOut[]>([])
  const scatter     = shallowRef<api.UmapPoint[]>([])
  const runs        = shallowRef<api.RunOut[]>([])
  const diagnostics = ref<api.DiagnosticsOut | null>(null)
  const loading     = ref(false)
  const notified    = new Set<string>()

  function hasRunningRun(): boolean {
    return runs.value.some(r => r.status === 'running')
  }

  function syncPolling() {
    if (hasRunningRun()) startPolling()
    else stopPolling()
  }

  function updateClusterInList(updated: api.ClusterOut) {
    list.value = list.value.map(c =>
      c.cluster_id === updated.cluster_id ? { ...c, ...updated } : c,
    )
  }

  async function loadClusters() {
    loading.value = true
    try { list.value = await api.listClusters() }
    catch (e) { notifyError(e) }
    finally { loading.value = false }
  }

  async function loadScatter() {
    try { scatter.value = await api.scatterPoints() }
    catch (e) { notifyError(e) }
  }

  async function loadRuns() {
    try {
      runs.value = await api.listRuns()
      syncPolling()
    } catch (e) { notifyError(e) }
  }

  async function pollRuns() {
    try {
      const prev = new Map(runs.value.map(r => [r.run_id, r.status]))
      runs.value = await api.listRuns()
      for (const r of runs.value) {
        const was = prev.get(r.run_id)
        const key = `${r.run_id}:${r.status}`
        if (was === 'running' && r.status !== 'running' && !notified.has(key)) {
          notified.add(key)
          if (r.status === 'finished') {
            useToastStore().push(`Run #${r.run_id} finished (${r.n_clusters} clusters)`, 'info')
          } else if (r.status === 'cancelled') {
            useToastStore().push(`Run #${r.run_id} stopped`, 'info')
          } else if (r.status === 'failed') {
            useToastStore().push(`Run #${r.run_id} failed: ${r.notes ?? 'unknown error'}`, 'error', 8000)
          }
        }
      }
      syncPolling()
    } catch { /* ignore transient poll errors */ }
  }

  const { start: startPolling, stop: stopPolling } = usePolling(pollRuns, POLL_MS)

  async function loadDiagnostics(runId: number) {
    try { diagnostics.value = await api.getDiagnostics(runId) }
    catch (e) { notifyError(e) }
  }

  async function accept(runId: number) {
    try {
      await api.acceptRun(runId)
      await loadRuns()
      await loadClusters()
      await loadScatter()
    } catch (e) { notifyError(e) }
  }

  async function reject(runId: number, notes: string) {
    try {
      await api.rejectRun(runId, notes)
      await loadRuns()
    } catch (e) { notifyError(e) }
  }

  async function trigger(params?: api.ClusterRunParams) {
    try {
      const res = await api.triggerRun(params)
      useToastStore().push(`Clustering run #${res.run_id} started…`, 'info', 4000)
      await loadRuns()
      startPolling()
    } catch (e) {
      notifyError(e)
    }
  }

  async function cancel(runId: number) {
    try {
      await api.cancelRun(runId)
      await pollRuns()
    } catch (e) { notifyError(e) }
  }

  async function applyTag(clusterId: number, tag: string) {
    try {
      const res = await api.applyClusterTag(clusterId, tag)
      useToastStore().push(
        `Tagged ${res.applied} doc${res.applied === 1 ? '' : 's'} with #${res.tag}`,
        'info',
      )
      return res
    } catch (e) {
      notifyError(e)
      throw e
    }
  }

  async function applyAllTags() {
    try {
      const res = await api.applyAllClusterTags()
      useToastStore().push(
        `Applied ${res.total_applied} tag${res.total_applied === 1 ? '' : 's'} across ${res.clusters.length} clusters`,
        'info',
      )
      return res
    } catch (e) {
      notifyError(e)
      throw e
    }
  }

  async function patchClusterLabel(clusterId: number, label: string) {
    try {
      const updated = await api.patchCluster(clusterId, { label })
      updateClusterInList(updated)
      return updated
    } catch (e) {
      notifyError(e)
      throw e
    }
  }

  async function regenerateLabel(clusterId: number) {
    try {
      const updated = await api.regenerateClusterLabel(clusterId)
      updateClusterInList(updated)
      const detail = updated.description
        ? `${updated.label} — ${updated.description}`
        : updated.label
      useToastStore().push(`Regenerated: ${detail}`, 'info', 6000)
      return updated
    } catch (e) {
      notifyError(e)
      throw e
    }
  }

  return {
    list, scatter, runs, diagnostics, loading,
    loadClusters, loadScatter, loadRuns, loadDiagnostics,
    accept, reject, trigger, cancel,
    applyTag, applyAllTags, patchClusterLabel, regenerateLabel,
    stopPolling, hasRunningRun,
  }
})
