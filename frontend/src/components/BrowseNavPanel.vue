<template>
  <aside class="browse-nav">
    <section class="browse-nav-section">
      <h2 class="browse-nav-heading">Sources</h2>
      <div class="browse-nav-chips">
        <span
          v-for="src in INGESTION_SOURCES"
          :key="src"
          class="chip"
          :class="{ active: store.sources.includes(src) }"
          @click="store.toggleSource(src)"
        >{{ SOURCE_LABELS[src] }}</span>
      </div>
    </section>

    <section class="browse-nav-section">
      <h2 class="browse-nav-heading">Source tags</h2>
      <input
        v-model="sourceQ"
        type="search"
        class="browse-nav-search"
        placeholder="Filter tags…"
      />
      <div v-if="store.loadingTags" class="browse-nav-hint">Loading…</div>
      <ul v-else class="browse-nav-list">
        <li
          v-for="t in filteredSourceTags"
          :key="t.tag"
          class="browse-nav-item"
          :class="{ active: store.sourceTags.includes(t.tag) }"
          @click="store.toggleSourceTag(t.tag)"
        >
          <span class="tag-pill tag-pill--source">{{ t.tag }}</span>
          <span class="browse-nav-count">{{ t.count }}</span>
        </li>
        <li v-if="!filteredSourceTags.length" class="browse-nav-hint">No source tags</li>
      </ul>
    </section>

    <section class="browse-nav-section">
      <h2 class="browse-nav-heading">Overlay tags</h2>
      <input
        v-model="overlayQ"
        type="search"
        class="browse-nav-search"
        placeholder="Filter tags…"
      />
      <div v-if="store.loadingTags" class="browse-nav-hint">Loading…</div>
      <ul v-else class="browse-nav-list">
        <li
          v-for="t in filteredOverlayTags"
          :key="`${t.tag}-${t.origin}`"
          class="browse-nav-item"
          :class="{ active: store.overlayTags.includes(t.tag) }"
          @click="store.toggleOverlayTag(t.tag)"
        >
          <span class="tag-pill" :class="`tag-pill--${t.origin}`">{{ t.tag }}</span>
          <span class="badge badge-sm" :class="`badge-${t.origin}`">{{ t.origin }}</span>
          <span class="browse-nav-count">{{ t.count }}</span>
        </li>
        <li v-if="!filteredOverlayTags.length" class="browse-nav-hint">No overlay tags</li>
      </ul>
    </section>
  </aside>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { INGESTION_SOURCES, SOURCE_LABELS } from '@/constants/sources'
import { useBrowseStore } from '@/stores/browse'

const store = useBrowseStore()
const sourceQ = ref('')
const overlayQ = ref('')

const filteredSourceTags = computed(() => {
  const q = sourceQ.value.trim().toLowerCase()
  if (!q) return store.tagRows.source
  return store.tagRows.source.filter(t => t.tag.toLowerCase().includes(q))
})

const filteredOverlayTags = computed(() => {
  const q = overlayQ.value.trim().toLowerCase()
  if (!q) return store.tagRows.overlay
  return store.tagRows.overlay.filter(t => t.tag.toLowerCase().includes(q))
})
</script>

<style scoped>
.browse-nav {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-height: 0;
  overflow: hidden;
}
.browse-nav-section {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-height: 0;
  flex: 1 1 0;
}
.browse-nav-section:first-child {
  flex: 0 0 auto;
}
.browse-nav-heading {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: .08em;
  color: var(--hint);
  font-weight: 500;
}
.browse-nav-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.browse-nav-search {
  width: 100%;
  padding: 6px 8px;
  border: 0.5px solid var(--border);
  border-radius: var(--radius);
  font-size: 12px;
  background: var(--surface);
}
.browse-nav-list {
  list-style: none;
  overflow-y: auto;
  min-height: 0;
  flex: 1;
  border: 0.5px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
}
.browse-nav-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 7px 10px;
  border-bottom: 0.5px solid var(--border);
  cursor: pointer;
  font-size: 12px;
}
.browse-nav-item:last-child { border-bottom: none }
.browse-nav-item:hover { background: #fafaf8 }
.browse-nav-item.active { background: #f0ede8 }
.browse-nav-count {
  margin-left: auto;
  font-size: 10px;
  color: var(--hint);
}
.browse-nav-hint {
  font-size: 12px;
  color: var(--hint);
  padding: 8px 2px;
}
.badge-sm {
  font-size: 9px;
  padding: 1px 5px;
}
</style>
