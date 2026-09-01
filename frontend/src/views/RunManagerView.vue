<template>
  <div>
    <div class="page-header">
      <div><h1 class="page-title">Cluster run manager</h1><p class="page-sub">Run history, diagnostics, and lifecycle suggestions</p></div>
      <button class="btn btn--primary" :disabled="store.hasRunningRun()" @click="dialogOpen = true">
        {{ store.hasRunningRun() ? 'Running…' : '+ New run' }}
      </button>
    </div>

    <ClusterRunDialog v-model:open="dialogOpen" :busy="triggering" @submit="trigger" />

    <div class="table-wrap mb-4">
      <table class="tag-table">
        <thead><tr><th>Run</th><th>Date</th><th>Algorithm</th><th>Clusters</th><th>Status</th><th></th></tr></thead>
        <tbody>
          <tr v-for="r in store.runs" :key="r.run_id" :class="{ 'row--active': r.accepted }">
            <td>#{{ r.run_id }}</td>
            <td>{{ new Date(r.timestamp * 1000).toLocaleDateString() }}</td>
            <td>{{ r.algorithm }}</td>
            <td>{{ r.status === 'running' ? '…' : r.n_clusters }}</td>
            <td><span class="run-badge" :class="statusClass(r)">{{ statusLabel(r) }}</span></td>
            <td class="right">
              <template v-if="r.status === 'running'">
                <button class="btn-xs btn-xs--danger" @click="store.cancel(r.run_id)">Stop</button>
              </template>
              <template v-else-if="r.status === 'finished'">
                <button class="btn-xs" @click="showDiag(r.run_id)">Diagnostics</button>
                <button class="btn-xs btn-xs--ok ml-1" @click="store.accept(r.run_id)">Accept</button>
                <button class="btn-xs btn-xs--danger ml-1" @click="store.reject(r.run_id, '')">Reject</button>
              </template>
              <span v-else-if="r.status === 'failed'" class="hint">{{ r.notes ?? 'Failed' }}</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <template v-if="diag">
      <h2 class="section-title">Diagnostics — Run #{{ diag.run_id }}</h2>
      <div class="diag-grid mb-3">
        <div class="metric-card"><div class="metric-val">{{ diag.n_clusters }}</div><div class="metric-lbl">Clusters</div></div>
        <div class="metric-card"><div class="metric-val">{{ diag.n_noise }}</div><div class="metric-lbl">Noise</div></div>
        <div class="metric-card"><div class="metric-val">{{ Object.values(diag.cluster_sizes).reduce((a,b)=>a+b,0) }}</div><div class="metric-lbl">Assigned</div></div>
      </div>

      <h3 class="section-title">Suggestions</h3>
      <div class="table-wrap">
        <div v-for="s in diag.merge_suggestions" :key="s.cluster_id_a + '-' + s.cluster_id_b" class="suggestion merge">
          <span class="sug-icon">↔</span>
          <div><strong>Merge?</strong> "{{ s.label_a }}" + "{{ s.label_b }}" · similarity {{ s.similarity }}</div>
        </div>
        <div v-for="d in diag.drift_flags.filter(f => f.flagged)" :key="d.cluster_id" class="suggestion split">
          <span class="sug-icon">↕</span>
          <div><strong>Split?</strong> "{{ d.label }}" · drift {{ d.drift_score }} · {{ d.n_recent }} recent docs</div>
        </div>
      </div>
    </template>
  </div>
</template>
<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import ClusterRunDialog from '@/components/ClusterRunDialog.vue'
import { useClustersStore } from '@/stores/clusters'
import type { ClusterRunParams, RunOut } from '@/api/client'

const store = useClustersStore()
const diag  = computed(() => store.diagnostics)
const dialogOpen  = ref(false)
const triggering  = ref(false)

onMounted(() => store.loadRuns())
onUnmounted(() => store.stopPolling())

function statusLabel(r: RunOut): string {
  if (r.accepted) return 'Active'
  if (r.status === 'running') return 'Running'
  if (r.status === 'finished') return 'Finished'
  if (r.status === 'cancelled') return 'Stopped'
  if (r.status === 'failed') return 'Failed'
  return r.status
}

function statusClass(r: RunOut): string {
  if (r.accepted) return 'run-badge--active'
  if (r.status === 'running') return 'run-badge--running'
  if (r.status === 'failed') return 'run-badge--failed'
  if (r.status === 'cancelled') return 'run-badge--cancelled'
  return 'run-badge--pending'
}

async function showDiag(id: number) { await store.loadDiagnostics(id) }

async function trigger(params: ClusterRunParams) {
  triggering.value = true
  try {
    await store.trigger(params)
    dialogOpen.value = false
  } finally {
    triggering.value = false
  }
}
</script>
