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

    <div v-if="source === 'image'" class="path-form">
      <label class="path-label">{{ SOURCE_PATH_LABELS[source] }}</label>
      <ul v-if="imageDirs.length" class="dir-list">
        <li v-for="dir in imageDirs" :key="dir.path" class="dir-row">
          <span class="dir-path" :title="dir.path">{{ dir.path }}</span>
          <span class="dir-status hint" :class="{ 'path-status--bad': !dir.exists }">
            {{ dir.exists ? '✓ found' : '⚠ not found' }}
          </span>
          <button
            class="btn-xs btn-xs--danger"
            type="button"
            :disabled="dirsBusy"
            @click="removeDir(dir.path)"
          >Remove</button>
        </li>
      </ul>
      <div v-else class="path-status hint">No image folders configured yet.</div>
      <div class="path-row">
        <input
          v-model="newDir"
          class="path-input"
          type="text"
          placeholder="Add another image folder…"
          :disabled="dirsBusy"
          @keydown.enter="addDir"
        />
        <button class="btn-xs" type="button" :disabled="dirsBusy" @click="browseNewDir">Browse…</button>
        <button class="btn-xs" type="button" :disabled="dirsBusy || !newDir.trim()" @click="addDir">Add</button>
      </div>
      <div v-if="dirBrowsing" class="path-status hint">Waiting for the file picker window to open…</div>
    </div>

    <div v-else class="path-form">
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

// ── Image folders (the image source is a list of folders) ───────────────────

const imageDirs = ref<api.ImageDir[]>([])
const newDir = ref('')
const dirsBusy = ref(false)
const dirBrowsing = ref(false)

async function loadImageDirs() {
  try {
    imageDirs.value = await api.getImageDirs()
  } catch (e) { notifyError(e) }
}

async function addDir() {
  const value = newDir.value.trim()
  if (!value || dirsBusy.value) return
  dirsBusy.value = true
  try {
    imageDirs.value = await api.addImageDir(value)
    newDir.value = ''
    await ingest.load(true)
  } catch (e) { notifyError(e) } finally { dirsBusy.value = false }
}

async function removeDir(path: string) {
  if (dirsBusy.value) return
  dirsBusy.value = true
  try {
    imageDirs.value = await api.removeImageDir(path)
    await ingest.load(true)
  } catch (e) { notifyError(e) } finally { dirsBusy.value = false }
}

async function browseNewDir() {
  dirsBusy.value = true
  dirBrowsing.value = true
  try {
    const res = await api.browseImageDir()
    if (res.path) newDir.value = res.path
  } catch (e) { notifyError(e) } finally { dirsBusy.value = false; dirBrowsing.value = false }
}

function loadForSource() {
  if (props.source === 'image') loadImageDirs()
  else loadPath()
}

watch(() => props.source, loadForSource)
onMounted(loadForSource)

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
.dir-list { list-style: none; margin: 0 0 8px; padding: 0; display: flex; flex-direction: column; gap: 4px }
.dir-row { display: flex; align-items: center; gap: 8px }
.dir-path { flex: 1; min-width: 0; font-family: monospace; font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap }
.dir-status { flex: 0 0 auto; white-space: nowrap }
</style>
