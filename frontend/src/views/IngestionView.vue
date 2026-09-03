<template>
  <div>
    <h1 class="page-title">Ingestion</h1>
    <p class="page-sub">Source connectors, sync state, and fetch errors</p>

    <div class="metric-row mb-4">
      <div class="metric-card"><div class="metric-val">{{ st?.total ?? '—' }}</div><div class="metric-lbl">Total docs</div></div>
      <div class="metric-card"><div class="metric-val" style="color:#A32D2D">{{ st?.unfetchable ?? '—' }}</div><div class="metric-lbl">Unfetchable</div></div>
      <div class="metric-card"><div class="metric-val">{{ st?.pending ?? '—' }}</div><div class="metric-lbl">Pending fetch</div></div>
    </div>

    <div class="source-grid">
      <RouterLink
        v-for="src in ui.sources"
        :key="src"
        :to="`/ingestion/${src}`"
        class="source-card"
      >
        <span class="dot" :style="{ background: SOURCE_COLORS[src] }" />
        <div class="source-card-body">
          <div class="source-card-name">{{ SOURCE_LABELS[src] }}</div>
          <div class="source-card-count">{{ st?.by_source[src] ?? 0 }} documents</div>
        </div>
        <span class="source-card-arrow">→</span>
      </RouterLink>
    </div>

    <DomainTopLists :data="ingest.domains" />

    <MaintenancePanel />

    <label class="experimental-toggle">
      <input
        type="checkbox"
        :checked="ui.showExperimentalSources"
        @change="ui.setShowExperimentalSources(($event.target as HTMLInputElement).checked)"
      />
      <span>
        Show experimental sources
        <span class="experimental-hint">({{ experimentalLabels }} — not functional yet)</span>
      </span>
    </label>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted } from 'vue'
import DomainTopLists from '@/components/DomainTopLists.vue'
import MaintenancePanel from '@/components/MaintenancePanel.vue'
import { EXPERIMENTAL_SOURCES, SOURCE_COLORS, SOURCE_LABELS } from '@/constants/sources'
import { useIngestionStore } from '@/stores/ingestion'
import { useUiStore } from '@/stores/ui'

const ingest = useIngestionStore()
const ui = useUiStore()
const st = computed(() => ingest.status)
const experimentalLabels = EXPERIMENTAL_SOURCES.map((s) => SOURCE_LABELS[s]).join(', ')

onMounted(() => ingest.load())
</script>

<style scoped>
.source-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 10px;
}
.source-card {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 14px 16px;
  background: var(--surface);
  border: 0.5px solid var(--border);
  border-radius: var(--radius-lg);
  text-decoration: none;
  color: inherit;
  transition: border-color .1s, background .1s;
}
.source-card:hover {
  border-color: rgba(0, 0, 0, .22);
  background: #fafaf8;
}
.source-card-body { flex: 1; min-width: 0 }
.source-card-name { font-size: 13px; font-weight: 500 }
.source-card-count { font-size: 11px; color: var(--hint); margin-top: 2px }
.source-card-arrow { font-size: 12px; color: var(--hint) }
.experimental-toggle {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 14px;
  font-size: 12px;
  color: var(--muted);
  cursor: pointer;
  user-select: none;
}
.experimental-hint { color: var(--hint) }
</style>
