<template>
  <nav class="sidebar">
    <div class="logo">PKA <span>knowledge archive</span></div>

    <div class="nav-section">Explore</div>
    <RouterLink to="/search"   class="nav-item"><IconSearch />Search</RouterLink>
    <RouterLink to="/browse"   class="nav-item"><IconBrowse />Browse</RouterLink>
    <RouterLink to="/clusters" class="nav-item"><IconCluster />Clusters</RouterLink>
    <RouterLink to="/trends"   class="nav-item"><IconTrend />Trends</RouterLink>
    <RouterLink to="/tags"     class="nav-item"><IconTag />Tags</RouterLink>

    <div class="nav-section">Manage</div>
    <RouterLink to="/runs" class="nav-item"><IconRun />Cluster runs</RouterLink>

    <div class="nav-group" :class="{ 'nav-group--open': ingestionOpen }">
      <RouterLink
        to="/ingestion"
        class="nav-item nav-item--parent"
        :class="{ 'router-link-active': isIngestionActive }"
        @click="ingestionOpen = true"
      >
        <IconIngest />Ingestion
        <button
          type="button"
          class="nav-chevron"
          :aria-expanded="ingestionOpen"
          aria-label="Toggle ingestion sources"
          @click.prevent.stop="ingestionOpen = !ingestionOpen"
        >
          <IconChevron />
        </button>
      </RouterLink>
      <div v-show="ingestionOpen" class="nav-submenu">
        <RouterLink
          v-for="src in INGESTION_SOURCES"
          :key="src"
          :to="`/ingestion/${src}`"
          class="nav-item nav-item--sub"
        >
          <span class="dot" :style="{ background: SOURCE_COLORS[src] }" />
          {{ SOURCE_LABELS[src] }}
          <span class="count">{{ counts[src] ?? '—' }}</span>
        </RouterLink>
      </div>
    </div>

    <RouterLink to="/lists" class="nav-item"><IconList />Reading lists</RouterLink>

    <div class="nav-divider" />
    <div class="nav-section">Sources</div>
    <div
      v-for="src in INGESTION_SOURCES"
      :key="`stat-${src}`"
      class="nav-item source-stat"
    >
      <span class="dot" :style="{ background: SOURCE_COLORS[src] }" />{{ SOURCE_LABELS[src] }}
      <span class="count">{{ counts[src] ?? '—' }}</span>
    </div>
  </nav>
</template>

<script setup lang="ts">
import { onMounted, computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { INGESTION_SOURCES, SOURCE_COLORS, SOURCE_LABELS } from '@/constants/sources'
import { useIngestionStore } from '@/stores/ingestion'

const IconSearch  = { template: `<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="6.5" cy="6.5" r="4"/><line x1="10" y1="10" x2="14" y2="14"/></svg>` }
const IconBrowse  = { template: `<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="2" y="2" width="5" height="5" rx="1"/><rect x="9" y="2" width="5" height="5" rx="1"/><rect x="2" y="9" width="5" height="5" rx="1"/><rect x="9" y="9" width="5" height="5" rx="1"/></svg>` }
const IconCluster = { template: `<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="5" cy="8" r="2.5"/><circle cx="11" cy="5" r="2.5"/><circle cx="11" cy="11" r="2.5"/><line x1="7.5" y1="7" x2="9" y2="6"/><line x1="7.5" y1="9" x2="9" y2="10"/></svg>` }
const IconTrend   = { template: `<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><polyline points="2,12 6,7 9,9 14,3"/></svg>` }
const IconTag     = { template: `<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2 4h7l5 4-5 4H2V4z"/></svg>` }
const IconRun     = { template: `<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="2" y="3" width="12" height="10" rx="2"/><line x1="5" y1="7" x2="11" y2="7"/><line x1="5" y1="10" x2="8" y2="10"/></svg>` }
const IconIngest  = { template: `<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="2" y="2" width="12" height="12" rx="2"/><line x1="8" y1="5" x2="8" y2="11"/><line x1="5" y1="8" x2="11" y2="8"/></svg>` }
const IconList    = { template: `<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="2" y="2" width="10" height="13" rx="1.5"/><line x1="5" y1="6" x2="10" y2="6"/><line x1="5" y1="9" x2="10" y2="9"/><line x1="5" y1="12" x2="8" y2="12"/></svg>` }
const IconChevron = { template: `<svg class="icon icon-chevron" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><polyline points="4,6 8,10 12,6"/></svg>` }

const route = useRoute()
const ingest = useIngestionStore()
const ingestionOpen = ref(false)

onMounted(() => ingest.load())
const counts = computed(() => ingest.status?.by_source ?? {})

const isIngestionActive = computed(() => route.path.startsWith('/ingestion'))

watch(isIngestionActive, (active) => {
  if (active) ingestionOpen.value = true
}, { immediate: true })
</script>

<style scoped>
.sidebar     { background: #f0ede8; border-right: 0.5px solid var(--border); padding: 16px 0; display: flex; flex-direction: column }
.logo        { padding: 0 16px 16px; font-size: 15px; font-weight: 500; border-bottom: 0.5px solid var(--border); margin-bottom: 8px }
.logo span   { font-size: 11px; font-weight: 400; color: var(--hint); display: block; margin-top: 2px }
.nav-section { font-size: 10px; text-transform: uppercase; letter-spacing: .08em; color: var(--hint); padding: 10px 16px 4px; font-weight: 500 }
.nav-item    { padding: 8px 16px; font-size: 13px; color: var(--muted); display: flex; align-items: center; gap: 8px; text-decoration: none; transition: background .1s }
.nav-item:hover, .nav-item.router-link-active { background: var(--surface); color: var(--text); font-weight: 500 }
.nav-divider { height: 0.5px; background: var(--border); margin: 8px 0 }
.icon        { width: 14px; height: 14px; opacity: .6; flex-shrink: 0 }
.dot         { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0 }
.count       { margin-left: auto; font-size: 10px; color: var(--hint) }
.source-stat { cursor: default }

.nav-group { display: flex; flex-direction: column }
.nav-item--parent { position: relative; padding-right: 32px }
.nav-chevron {
  position: absolute;
  right: 8px;
  top: 50%;
  transform: translateY(-50%);
  display: flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  padding: 0;
  border: none;
  background: none;
  cursor: pointer;
  color: var(--hint);
  border-radius: 4px;
}
.nav-chevron:hover { background: rgba(0, 0, 0, .06); color: var(--text) }
.nav-group--open .nav-chevron .icon-chevron { transform: rotate(180deg) }
.nav-submenu { display: flex; flex-direction: column }
.nav-item--sub {
  padding-left: 34px;
  font-size: 12px;
}
</style>
