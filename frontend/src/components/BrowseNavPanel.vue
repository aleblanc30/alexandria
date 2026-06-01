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
        <span class="chip-sep" />
        <span
          class="chip"
          :class="{ active: store.waybackOnly }"
          title="Firefox bookmarks recovered from the Internet Archive"
          @click="store.toggleWayback()"
        >Wayback</span>
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
      <div v-else class="browse-nav-tags">
        <button
          v-for="t in filteredSourceTags"
          :key="t.tag"
          type="button"
          class="browse-nav-tag-btn tag-pill tag-pill--source"
          :class="{ active: store.sourceTags.includes(t.tag) }"
          @click="store.toggleSourceTag(t.tag)"
        >
          <span class="browse-nav-tag-label">#{{ t.tag }}</span>
          <span class="browse-nav-tag-count">{{ t.count }}</span>
        </button>
        <p v-if="!filteredSourceTags.length" class="browse-nav-hint">No source tags</p>
      </div>
    </section>

    <section class="browse-nav-section">
      <h2 class="browse-nav-heading">Level 1 topics</h2>
      <input
        v-model="level1Q"
        type="search"
        class="browse-nav-search"
        placeholder="Filter tags…"
      />
      <div v-if="store.loadingTags" class="browse-nav-hint">Loading…</div>
      <div v-else class="browse-nav-tags">
        <button
          v-for="t in filteredLevel1Tags"
          :key="t.tag"
          type="button"
          class="browse-nav-tag-btn tag-pill tag-pill--cluster_l1"
          :class="{ active: store.level1Tags.includes(t.tag) }"
          @click="store.toggleLevel1Tag(t.tag)"
        >
          <span class="browse-nav-tag-label">{{ t.tag }}</span>
          <span class="browse-nav-tag-count">{{ t.count }}</span>
        </button>
        <p v-if="!filteredLevel1Tags.length" class="browse-nav-hint">No level 1 tags</p>
      </div>
    </section>

    <section class="browse-nav-section">
      <h2 class="browse-nav-heading">Level 2 subtopics</h2>
      <input
        v-model="level2Q"
        type="search"
        class="browse-nav-search"
        placeholder="Filter tags…"
      />
      <div v-if="store.loadingTags" class="browse-nav-hint">Loading…</div>
      <div v-else class="browse-nav-tags">
        <button
          v-for="t in filteredLevel2Tags"
          :key="t.tag"
          type="button"
          class="browse-nav-tag-btn tag-pill tag-pill--cluster_l2"
          :class="{ active: store.level2Tags.includes(t.tag) }"
          @click="store.toggleLevel2Tag(t.tag)"
        >
          <span class="browse-nav-tag-label">{{ t.tag }}</span>
          <span class="browse-nav-tag-count">{{ t.count }}</span>
        </button>
        <p v-if="!filteredLevel2Tags.length" class="browse-nav-hint">No level 2 tags</p>
      </div>
    </section>
  </aside>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { INGESTION_SOURCES, SOURCE_LABELS } from '@/constants/sources'
import { useBrowseStore } from '@/stores/browse'

const store = useBrowseStore()
const sourceQ = ref('')
const level1Q = ref('')
const level2Q = ref('')

const filteredSourceTags = computed(() => {
  const q = sourceQ.value.trim().toLowerCase()
  if (!q) return store.tagRows.source
  return store.tagRows.source.filter(t => t.tag.toLowerCase().includes(q))
})

const filteredLevel1Tags = computed(() => {
  const q = level1Q.value.trim().toLowerCase()
  if (!q) return store.tagRows.level1
  return store.tagRows.level1.filter(t => t.tag.toLowerCase().includes(q))
})

const filteredLevel2Tags = computed(() => {
  const q = level2Q.value.trim().toLowerCase()
  if (!q) return store.tagRows.level2
  return store.tagRows.level2.filter(t => t.tag.toLowerCase().includes(q))
})
</script>

<style scoped>
.browse-nav {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.browse-nav-section {
  display: flex;
  flex-direction: column;
  gap: 8px;
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
  align-items: center;
}
.browse-nav-chips .chip-sep {
  width: 0.5px;
  height: 16px;
  background: var(--border);
  flex-shrink: 0;
}
.browse-nav-search {
  width: 100%;
  padding: 6px 8px;
  border: 0.5px solid var(--border);
  border-radius: var(--radius);
  font-size: 12px;
  background: var(--surface);
}
.browse-nav-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
}
.browse-nav-tag-btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 3px 8px;
  border: 1px solid transparent;
  border-radius: 10px;
  font-size: 11px;
  font-weight: 500;
  line-height: 1.3;
  cursor: pointer;
  transition: border-color .12s, box-shadow .12s;
}
.browse-nav-tag-btn:hover {
  border-color: rgba(0, 0, 0, .14);
}
.browse-nav-tag-btn.active {
  border-color: rgba(0, 0, 0, .28);
  box-shadow: 0 0 0 1px rgba(0, 0, 0, .06);
}
.browse-nav-tag-label {
  max-width: 160px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.browse-nav-tag-count {
  font-size: 10px;
  font-weight: 400;
  opacity: .72;
  font-variant-numeric: tabular-nums;
}
.browse-nav-hint {
  font-size: 12px;
  color: var(--hint);
  margin: 0;
  padding: 2px 0;
}
</style>
