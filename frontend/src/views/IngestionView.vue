<template>
  <div>
    <h1 class="page-title">Ingestion status</h1>
    <p class="page-sub">Source connectors, sync state, and fetch errors</p>

    <div class="metric-row mb-4">
      <div class="metric-card"><div class="metric-val">{{ st?.total ?? '—' }}</div><div class="metric-lbl">Total docs</div></div>
      <div class="metric-card"><div class="metric-val" style="color:#A32D2D">{{ st?.unfetchable ?? '—' }}</div><div class="metric-lbl">Unfetchable</div></div>
      <div class="metric-card"><div class="metric-val">{{ st?.pending ?? '—' }}</div><div class="metric-lbl">Pending fetch</div></div>
    </div>

    <div class="card mb-4">
      <div v-for="src in sources" :key="src" class="source-row">
        <span class="dot" :style="{ background: srcColor[src] }" />
        <div class="source-info">
          <div class="source-name">{{ src }}</div>
          <div class="source-count">{{ st?.by_source[src] ?? 0 }} documents</div>
          <div v-if="fetchSummary(src)" class="fetch-summary hint">{{ fetchSummary(src) }}</div>
          <div class="phase-bars">
            <div
              v-for="phase in phaseList(src)"
              :key="phase.name"
              class="phase-bar"
            >
              <div class="phase-bar-head">
                <span class="phase-bar-label">{{ phaseLabel(phase.name) }}</span>
                <span class="phase-bar-count">{{ phaseCountLabel(phase) }}</span>
              </div>
              <div class="progress-track">
                <div
                  class="progress-fill"
                  :class="{
                    'progress-fill--indeterminate': phaseIndeterminate(phase, src),
                    'progress-fill--active': phase.active,
                  }"
                  :style="phaseBarStyle(phase, src)"
                />
              </div>
            </div>
            <div v-if="jobError(src)" class="phase-error">{{ jobError(src) }}</div>
          </div>
        </div>
        <div class="source-actions">
          <template v-if="ingest.isMetadataRunning(src)">
            <button class="btn-xs btn-xs--danger" @click="ingest.cancel(src)">Stop</button>
          </template>
          <button
            v-else
            class="btn-xs"
            :disabled="ingest.isIngestRunning(src)"
            @click="ingest.syncMetadata(src)"
          >
            Sync metadata
          </button>
          <template v-if="ingest.isIngestRunning(src)">
            <button class="btn-xs btn-xs--danger" @click="ingest.cancel(src)">Stop</button>
          </template>
          <button
            v-else
            class="btn-xs"
            :disabled="ingest.isMetadataRunning(src)"
            @click="ingest.ingest(src)"
          >
            Ingest
          </button>
        </div>
      </div>
    </div>

    <h2 class="section-title">Unfetchable bookmarks <span class="hint">({{ ingest.unfetchable.length }})</span></h2>
    <div class="table-wrap">
      <div v-for="u in ingest.unfetchable.slice(0, 50)" :key="u.id" class="unfetch-row">
        <span class="unfetch-url">{{ u.url }}</span>
        <span class="err-badge">{{ u.http_status ?? 'timeout' }}</span>
        <span class="hint">{{ u.error }}</span>
      </div>
    </div>
  </div>
</template>
<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { useIngestionStore } from '@/stores/ingestion'
import type { PhaseDetail, SyncProgress } from '@/api/client'

const ingest  = useIngestionStore()
const st      = computed(() => ingest.status)
const sources = ['firefox','zotero','calibre','image']
const srcColor: Record<string,string> = {
  firefox: '#378ADD', zotero: '#639922', calibre: '#BA7517', image: '#7F77DD',
}

const PHASE_ORDER = ['metadata', 'fetching', 'embedding'] as const
const PHASE_LABELS: Record<string, string> = {
  metadata: 'Metadata',
  fetching: 'Fetching',
  embedding: 'Embedding',
}

onMounted(() => ingest.load())

function progressFor(src: string): SyncProgress | undefined {
  return ingest.progress[src]
}

function defaultPhases(): PhaseDetail[] {
  return PHASE_ORDER.map(name => ({
    name, total: 0, processed: 0, percent: 0, active: false,
  }))
}

function phaseList(src: string): PhaseDetail[] {
  const p = progressFor(src)
  if (p?.phase_details?.length) return p.phase_details
  return defaultPhases()
}

function fetchSummary(src: string): string | null {
  if (src !== 'firefox') return null
  const fb = st.value?.fetch_by_source?.[src]
  if (!fb) return null
  const parts = [
    `${fb.fetched ?? 0} fetched`,
    `${fb.unfetchable ?? 0} unfetchable`,
    `${fb.skipped ?? 0} skipped`,
    `${fb.embedded ?? 0} embedded`,
  ]
  if ((fb.pending ?? 0) > 0) parts.push(`${fb.pending} pending`)
  return parts.join(' · ')
}

function phaseLabel(name: string): string {
  return PHASE_LABELS[name] ?? name
}

function phaseCountLabel(phase: PhaseDetail): string {
  if (phase.name === 'fetching' && phase.total === 0) return '—'
  if (phase.total === 0) return '0'
  return `${phase.processed} / ${phase.total}`
}

function phaseIndeterminate(phase: PhaseDetail, src: string): boolean {
  const p = progressFor(src)
  return !!p && p.status === 'running' && phase.active && phase.total === 0
}

function phaseBarStyle(phase: PhaseDetail, src: string): Record<string, string> {
  if (phaseIndeterminate(phase, src)) return {}
  return { width: `${phase.percent}%` }
}

function jobError(src: string): string | null {
  const p = progressFor(src)
  if (!p || p.status !== 'error') return null
  return p.error ?? 'Job failed'
}
</script>
