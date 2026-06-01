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
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { PhaseDetail, SyncProgress } from '@/api/client'
import { SOURCE_COLORS, SOURCE_LABELS, ingestJobLabel, sourceHasFetchPhase, type IngestionSource } from '@/constants/sources'
import { ingestStatsSummary } from '@/lib/ingestStats'
import { useIngestionStore } from '@/stores/ingestion'

const props = defineProps<{ source: IngestionSource }>()

const ingest = useIngestionStore()
const st = computed(() => ingest.status)

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
  const phases = p?.phase_details?.length ? p.phase_details : defaultPhases()
  if (sourceHasFetchPhase(props.source)) return phases
  return phases.filter(ph => ph.name !== 'fetching')
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
