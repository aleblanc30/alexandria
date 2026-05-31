<template>
  <nav
    class="sidebar"
    :class="{
      'sidebar--collapsed': collapsed && !overlay,
      'sidebar--overlay': overlay,
    }"
  >
    <div v-if="overlay" class="sidebar-peek" aria-hidden="true">
      <IconChevronRight />
    </div>

    <div class="sidebar-body">
      <div class="logo" :title="showFull ? undefined : 'PKA knowledge archive'">
        PKA <span v-if="showFull">knowledge archive</span>
      </div>

      <div v-if="showFull" class="nav-section">Explore</div>
      <RouterLink to="/search"   class="nav-item" title="Search"><IconSearch /><span v-if="showFull" class="nav-label">Search</span></RouterLink>
      <RouterLink to="/browse"   class="nav-item" title="Browse"><IconBrowse /><span v-if="showFull" class="nav-label">Browse</span></RouterLink>
      <RouterLink to="/clusters" class="nav-item" title="Clusters"><IconCluster /><span v-if="showFull" class="nav-label">Clusters</span></RouterLink>
      <RouterLink to="/trends"   class="nav-item" title="Trends"><IconTrend /><span v-if="showFull" class="nav-label">Trends</span></RouterLink>
      <RouterLink to="/tags"     class="nav-item" title="Tags"><IconTag /><span v-if="showFull" class="nav-label">Tags</span></RouterLink>

      <div v-if="showFull" class="nav-section">Manage</div>
      <RouterLink to="/runs" class="nav-item" title="Cluster runs"><IconRun /><span v-if="showFull" class="nav-label">Cluster runs</span></RouterLink>

      <template v-if="showFull">
        <div class="nav-group" :class="{ 'nav-group--open': ingestionOpen }">
          <RouterLink
            to="/ingestion"
            class="nav-item nav-item--parent"
            :class="{ 'router-link-active': isIngestionActive }"
            @click="ingestionOpen = true"
          >
            <IconIngest /><span class="nav-label">Ingestion</span>
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
      </template>
      <RouterLink v-else to="/ingestion" class="nav-item" title="Ingestion"><IconIngest /></RouterLink>

      <RouterLink to="/lists" class="nav-item" title="Reading lists"><IconList /><span v-if="showFull" class="nav-label">Reading lists</span></RouterLink>

      <template v-if="showFull">
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
      </template>
    </div>
  </nav>
</template>

<script setup lang="ts">
import { onMounted, computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { INGESTION_SOURCES, SOURCE_COLORS, SOURCE_LABELS } from '@/constants/sources'
import { useIngestionStore } from '@/stores/ingestion'

const props = defineProps<{ collapsed?: boolean; overlay?: boolean }>()

const showFull = computed(() => props.overlay || !props.collapsed)

const IconSearch  = { template: `<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="6.5" cy="6.5" r="4"/><line x1="10" y1="10" x2="14" y2="14"/></svg>` }
const IconBrowse  = { template: `<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="2" y="2" width="5" height="5" rx="1"/><rect x="9" y="2" width="5" height="5" rx="1"/><rect x="2" y="9" width="5" height="5" rx="1"/><rect x="9" y="9" width="5" height="5" rx="1"/></svg>` }
const IconCluster = { template: `<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="5" cy="8" r="2.5"/><circle cx="11" cy="5" r="2.5"/><circle cx="11" cy="11" r="2.5"/><line x1="7.5" y1="7" x2="9" y2="6"/><line x1="7.5" y1="9" x2="9" y2="10"/></svg>` }
const IconTrend   = { template: `<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><polyline points="2,12 6,7 9,9 14,3"/></svg>` }
const IconTag     = { template: `<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2 4h7l5 4-5 4H2V4z"/></svg>` }
const IconRun     = { template: `<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="2" y="3" width="12" height="10" rx="2"/><line x1="5" y1="7" x2="11" y2="7"/><line x1="5" y1="10" x2="8" y2="10"/></svg>` }
const IconIngest  = { template: `<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="2" y="2" width="12" height="12" rx="2"/><line x1="8" y1="5" x2="8" y2="11"/><line x1="5" y1="8" x2="11" y2="8"/></svg>` }
const IconList    = { template: `<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="2" y="2" width="10" height="13" rx="1.5"/><line x1="5" y1="6" x2="10" y2="6"/><line x1="5" y1="9" x2="10" y2="9"/><line x1="5" y1="12" x2="8" y2="12"/></svg>` }
const IconChevron = { template: `<svg class="icon icon-chevron" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><polyline points="4,6 8,10 12,6"/></svg>` }
const IconChevronRight = { template: `<svg class="icon icon-chevron-right" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><polyline points="6,4 10,8 6,12"/></svg>` }

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
.sidebar--collapsed { padding: 12px 0; align-items: center }
.sidebar--overlay {
  position: fixed;
  left: 0;
  top: 0;
  bottom: 0;
  z-index: 200;
  width: 14px;
  padding: 0;
  overflow: hidden;
  transition: width 0.22s ease, box-shadow 0.22s ease;
  box-shadow: none;
  border-right: 0.5px solid var(--border);
}
.sidebar--overlay:hover {
  width: 200px;
  box-shadow: 4px 0 16px rgba(0, 0, 0, .10);
}
.sidebar-peek {
  position: absolute;
  inset: 0;
  width: 14px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #e8e4dc;
  color: var(--hint);
  pointer-events: none;
  transition: opacity 0.15s ease;
}
.sidebar--overlay:hover .sidebar-peek { opacity: 0 }
.icon-chevron-right { width: 10px; height: 10px; opacity: .7 }
.sidebar-body {
  width: 200px;
  min-height: 100%;
  display: flex;
  flex-direction: column;
  opacity: 1;
}
.sidebar--overlay .sidebar-body {
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.15s ease 0.05s;
  padding: 16px 0;
}
.sidebar--overlay:hover .sidebar-body {
  opacity: 1;
  pointer-events: auto;
}
.logo        { padding: 0 16px 16px; font-size: 15px; font-weight: 500; border-bottom: 0.5px solid var(--border); margin-bottom: 8px; width: 100% }
.sidebar--collapsed .logo { padding: 0 0 12px; text-align: center; font-size: 13px }
.logo span   { font-size: 11px; font-weight: 400; color: var(--hint); display: block; margin-top: 2px }
.nav-section { font-size: 10px; text-transform: uppercase; letter-spacing: .08em; color: var(--hint); padding: 10px 16px 4px; font-weight: 500; width: 100% }
.nav-item    { padding: 8px 16px; font-size: 13px; color: var(--muted); display: flex; align-items: center; gap: 8px; text-decoration: none; transition: background .1s; width: 100% }
.sidebar--collapsed:not(.sidebar--overlay) .nav-item { padding: 10px 0; justify-content: center; width: 48px }
.nav-item:hover, .nav-item.router-link-active { background: var(--surface); color: var(--text); font-weight: 500 }
.nav-divider { height: 0.5px; background: var(--border); margin: 8px 0; width: 100% }
.icon        { width: 14px; height: 14px; opacity: .6; flex-shrink: 0 }
.dot         { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0 }
.count       { margin-left: auto; font-size: 10px; color: var(--hint) }
.source-stat { cursor: default }

.nav-group { display: flex; flex-direction: column; width: 100% }
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
