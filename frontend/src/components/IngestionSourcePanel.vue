<template>
  <div class="card">
    <div class="source-row">
      <span class="dot" :style="{ background: SOURCE_COLORS[source] }" />
      <div class="source-info">
        <div class="source-name">{{ SOURCE_LABELS[source] }}</div>
        <div class="source-count">{{ st?.by_source[source] ?? 0 }} documents</div>
        <div v-if="unavailableHint" class="source-unavailable hint">{{ unavailableHint }}</div>
        <div v-if="ingestSummary" class="fetch-summary hint">{{ ingestSummary }}</div>
        <div class="phase-bars">
          <div
            v-for="phase in phaseList"
            :key="phase.name"
            class="phase-bar"
          >
            <div class="phase-bar-head">
              <span class="phase-bar-label">{{ phaseLabel(phase.name) }}</span>
              <span class="phase-bar-count">{{ phaseCountLabel(phase) }}</span>
            </div>
            <div class="progress-track" :class="{ 'progress-track--stacked': hasFetchBreakdown(phase) }">
              <template v-if="hasFetchBreakdown(phase)">
                <div
                  class="progress-seg progress-seg--success"
                  :style="fetchSegStyle(phase, 'success')"
                />
                <div
                  class="progress-seg progress-seg--failure"
                  :style="fetchSegStyle(phase, 'failure')"
                />
                <div
                  class="progress-seg progress-seg--pending"
                  :style="fetchSegStyle(phase, 'pending')"
                />
              </template>
              <div
                v-else
                class="progress-fill"
                :class="{
                  'progress-fill--indeterminate': phaseIndeterminate(phase),
                  'progress-fill--active': phase.active,
                }"
                :style="phaseBarStyle(phase)"
              />
            </div>
          </div>
          <div v-if="jobError" class="phase-error">{{ jobError }}</div>
        </div>
      </div>
      <div class="source-actions">
        <template v-if="ingest.isMetadataRunning(source)">
          <button class="btn-xs btn-xs--danger" @click="ingest.cancel(source)">Stop</button>
        </template>
        <button
          v-else
          class="btn-xs"
          :disabled="ingest.isIngestRunning(source)"
          @click="ingest.syncMetadata(source)"
        >
          Sync metadata
        </button>
        <template v-if="ingest.isIngestRunning(source)">
          <button class="btn-xs btn-xs--danger" @click="ingest.cancel(source)">Stop</button>
        </template>
        <button
          v-else
          class="btn-xs"
          :disabled="ingest.isMetadataRunning(source)"
          @click="ingest.ingest(source)"
        >
          {{ ingestJobLabel(source) }}
        </button>
      </div>
    </div>

    <div class="path-form">
      <label class="path-label">{{ SOURCE_PATH_LABELS[source] }}</label>
      <div class="path-row">
        <input
          v-model="pathInput"
          class="path-input"
          type="text"
          :disabled="pathBusy"
          @keydown.enter="savePath"
        />
        <button class="btn-xs" type="button" :disabled="pathBusy" @click="browsePath">Browse…</button>
        <button class="btn-xs" type="button" :disabled="pathBusy || !pathDirty" @click="savePath">Save</button>
      </div>
      <div v-if="browsing" class="path-status hint">Waiting for the file picker window to open…</div>
      <div v-else-if="pathInfo" class="path-status hint" :class="{ 'path-status--bad': !pathInfo.exists }">
        {{ pathInfo.exists ? '✓ found on disk' : '⚠ not found on disk' }}
      </div>
    </div>

    <div class="danger-zone">
      <div class="danger-info">
        <div class="danger-label">Purge {{ SOURCE_LABELS[source] }}</div>
        <div class="hint">Delete every archived document, chunk, tag, and vector for this source. Cannot be undone.</div>
      </div>
      <button
        class="btn-xs btn-xs--danger"
        type="button"
        :disabled="purgeBusy || jobRunning || docCount === 0"
        @click="purge"
      >
        {{ purgeBusy ? 'Purging…' : 'Purge' }}
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import * as api from '@/api/client'
import type { PhaseDetail, SyncProgress } from '@/api/client'
import { SOURCE_COLORS, SOURCE_LABELS, SOURCE_PATH_LABELS, ingestJobLabel, sourceHasFetchPhase, sourceSkipsEmbedPhase, type IngestionSource } from '@/constants/sources'
import { ingestStatsSummary } from '@/lib/ingestStats'
import { notifyError } from '@/lib/notifyError'
import { useIngestionStore } from '@/stores/ingestion'

const props = defineProps<{ source: IngestionSource }>()

const ingest = useIngestionStore()
const st = computed(() => ingest.status)

// ── Source path (folder/database) config ────────────────────────────────────

const pathInfo = ref<api.SourcePathInfo | null>(null)
const pathInput = ref('')
const pathBusy = ref(false)
const browsing = ref(false)

const pathDirty = computed(() =>
  pathInfo.value != null && pathInput.value.trim() !== pathInfo.value.path,
)

async function loadPath() {
  try {
    pathInfo.value = await api.getSourcePath(props.source)
    pathInput.value = pathInfo.value.path
  } catch (e) { notifyError(e) }
}

async function browsePath() {
  pathBusy.value = true
  browsing.value = true
  try {
    const res = await api.browseSourcePath(props.source)
    if (res.path) pathInput.value = res.path
  } catch (e) { notifyError(e) } finally { pathBusy.value = false; browsing.value = false }
}

async function savePath() {
  const value = pathInput.value.trim()
  if (!value || pathBusy.value) return
  pathBusy.value = true
  try {
    pathInfo.value = await api.setSourcePath(props.source, value)
    pathInput.value = pathInfo.value.path
    await ingest.load(true)
  } catch (e) { notifyError(e) } finally { pathBusy.value = false }
}

watch(() => props.source, loadPath)
onMounted(loadPath)

// ── Purge ────────────────────────────────────────────────────────────────────

const purgeBusy = ref(false)
const docCount = computed(() => st.value?.by_source[props.source] ?? 0)
const jobRunning = computed(() =>
  ingest.isMetadataRunning(props.source) || ingest.isIngestRunning(props.source),
)

async function purge() {
  if (purgeBusy.value) return
  const label = SOURCE_LABELS[props.source]
  const ok = window.confirm(
    `Permanently delete all ${docCount.value} ${label} document(s) and their tags, chunks, and vectors?\n\nThis cannot be undone.`,
  )
  if (!ok) return
  purgeBusy.value = true
  try {
    await ingest.purge(props.source)
  } finally {
    purgeBusy.value = false
  }
}

const PHASE_ORDER = ['metadata', 'fetching', 'embedding'] as const
const PHASE_LABELS: Record<string, string> = {
  metadata: 'Metadata',
  fetching: 'Fetching',
  embedding: 'Embedding',
}

function progressFor(): SyncProgress | undefined {
  return ingest.progress[props.source]
}

function defaultPhases(): PhaseDetail[] {
  return PHASE_ORDER.map(name => ({
    name, total: 0, processed: 0, percent: 0, active: false,
  }))
}

const phaseList = computed(() => {
  const p = progressFor()
  let phases = p?.phase_details?.length ? p.phase_details : defaultPhases()
  if (sourceSkipsEmbedPhase(props.source)) {
    phases = phases.filter(ph => ph.name !== 'embedding')
  }
  if (!sourceHasFetchPhase(props.source)) {
    phases = phases.filter(ph => ph.name !== 'fetching')
  }
  return phases
})

const ingestSummary = computed(() => ingestStatsSummary(props.source, st.value))

const unavailableHint = computed((): string | null => {
  const msg = st.value?.source_unavailable?.[props.source]
  return msg ?? null
})

const jobError = computed((): string | null => {
  const p = progressFor()
  if (!p || p.status !== 'error') return null
  return p.error ?? 'Job failed'
})

function phaseLabel(name: string): string {
  if (name === 'fetching' && sourceHasFetchPhase(props.source)) return 'Fetch & embed'
  return PHASE_LABELS[name] ?? name
}

function phaseCountLabel(phase: PhaseDetail): string {
  if (phase.name === 'fetching' && phase.total === 0) return '—'
  if (phase.total === 0) return '0'
  return `${phase.processed} / ${phase.total}`
}

function phaseIndeterminate(phase: PhaseDetail): boolean {
  const p = progressFor()
  return !!p && p.status === 'running' && phase.active && phase.total === 0
}

function hasFetchBreakdown(phase: PhaseDetail): boolean {
  return props.source === 'firefox' && phase.name === 'fetching' && !!phase.breakdown
}

function fetchSegStyle(phase: PhaseDetail, key: 'success' | 'failure' | 'pending'): Record<string, string> {
  const b = phase.breakdown
  if (!b || phase.total <= 0) return { width: '0%' }
  const pct = Math.max(0, Math.min(100, Math.round(100 * b[key] / phase.total)))
  return { width: `${pct}%` }
}

function phaseBarStyle(phase: PhaseDetail): Record<string, string> {
  if (phaseIndeterminate(phase)) return {}
  return { width: `${phase.percent}%` }
}
</script>

<style scoped>
.path-form { margin-top: 14px; padding-top: 14px; border-top: 0.5px solid var(--border) }
.path-label { display: block; font-size: 12px; color: var(--muted); margin-bottom: 6px }
.path-row { display: flex; gap: 8px }
.path-input { flex: 1; min-width: 0; font-family: monospace; font-size: 12px }
.path-status { margin-top: 6px }
.path-status--bad { color: #A32D2D }
.danger-zone {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 14px;
  padding-top: 14px;
  border-top: 0.5px solid var(--border);
}
.danger-info { flex: 1; min-width: 0 }
.danger-label { font-size: 12px; font-weight: 500; color: #A32D2D; margin-bottom: 2px }
</style>
