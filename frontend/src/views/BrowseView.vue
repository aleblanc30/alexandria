<template>
  <div class="browse-layout">
    <BrowseNavPanel />

    <div class="browse-content">
      <div class="browse-header">
        <div>
          <h1 class="page-title">Browse &amp; search</h1>
          <p class="page-sub">Filter your archive or search by keyword and meaning</p>
        </div>
        <div class="view-toggle" role="group" aria-label="Result layout">
          <button
            type="button"
            class="view-toggle-btn"
            :class="{ active: store.viewMode === 'cards' }"
            @click="store.setViewMode('cards')"
          >Cards</button>
          <button
            type="button"
            class="view-toggle-btn"
            :class="{ active: store.viewMode === 'lines' }"
            @click="store.setViewMode('lines')"
          >Lines</button>
        </div>
      </div>

      <div class="search-bar">
        <input
          v-model="search.query"
          placeholder="Search by keyword or meaning…"
          @keyup.enter="runSearch()"
        />
        <button type="button" @click="runSearch()">Search</button>
      </div>

      <div class="filter-row">
        <span
          v-for="m in modes"
          :key="m"
          class="chip"
          :class="{ active: search.mode === m }"
          @click="setMode(m)"
        >{{ m }}</span>
        <span class="chip-sep" />
        <span
          class="chip"
          :class="{ active: search.includeImgs }"
          @click="toggleIncludeImages"
        >+images</span>
      </div>

      <p class="results-meta">
        <template v-if="isSearching && search.loading">Searching…</template>
        <template v-else-if="!isSearching && store.loading">Loading…</template>
        <template v-else-if="isSearching">
          {{ search.total }} result{{ search.total === 1 ? '' : 's' }}
        </template>
        <template v-else>
          {{ store.total.toLocaleString() }} document{{ store.total === 1 ? '' : 's' }}
          <span v-if="store.waybackOnly" class="hint"> · Wayback archive</span>
        </template>
      </p>

      <div v-if="showLoading" class="browse-loading">
        {{ isSearching ? 'Searching…' : 'Loading documents…' }}
      </div>

      <p
        v-else-if="showEmpty"
        class="browse-empty hint"
      >
        {{ isSearching ? 'No results match your search and filters.' : 'No documents match the current filters.' }}
      </p>

      <div v-else-if="store.viewMode === 'cards'" class="doc-grid">
        <template v-if="isSearching">
          <DocGridCard
            v-for="doc in search.results"
            :key="doc.id"
            :doc="toGridItem(doc)"
            :selected="ui.activeDocId === doc.id"
            @click="openDoc(doc.id)"
          />
        </template>
        <template v-else>
          <DocGridCard
            v-for="doc in store.documents"
            :key="doc.id"
            :doc="doc"
            :selected="ui.activeDocId === doc.id"
            @click="openDoc(doc.id)"
          />
        </template>
      </div>

      <div v-else class="doc-lines">
        <template v-if="isSearching">
          <DocCard
            v-for="doc in search.results"
            :key="doc.id"
            :doc="doc"
            :selected="ui.activeDocId === doc.id"
            @click="openDoc(doc.id)"
          />
        </template>
        <template v-else>
          <DocCard
            v-for="doc in store.documents"
            :key="doc.id"
            :doc="doc"
            :selected="ui.activeDocId === doc.id"
            @click="openDoc(doc.id)"
          />
        </template>
      </div>

      <div v-if="showSentinel" ref="sentinel" class="browse-sentinel">
        <button
          v-if="hasMore && !loadingAny"
          class="btn browse-load-more"
          :disabled="loadingMore"
          @click="loadMore()"
        >
          {{ loadingMore ? 'Loading…' : 'Load more' }}
        </button>
      </div>

      <p v-if="displayError" class="error">{{ displayError }}</p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { getDocument } from '@/api/client'
import { toGridItem } from '@/lib/docDisplay'
import { useBrowseStore } from '@/stores/browse'
import { useSearchStore } from '@/stores/search'
import { useUiStore } from '@/stores/ui'
import BrowseNavPanel from '@/components/BrowseNavPanel.vue'
import DocCard from '@/components/DocCard.vue'
import DocGridCard from '@/components/DocGridCard.vue'

const store = useBrowseStore()
const search = useSearchStore()
const ui = useUiStore()
const sentinel = ref<HTMLElement | null>(null)

const modes = ['semantic', 'fulltext', 'hybrid'] as const

const isSearching = computed(() => search.query.trim().length > 0)

const resultCount = computed(() =>
  isSearching.value ? search.results.length : store.documents.length,
)

const hasMore = computed(() =>
  isSearching.value
    ? search.results.length < search.total
    : store.documents.length < store.total,
)

const loadingAny = computed(() =>
  isSearching.value ? search.loading : store.loading,
)

const loadingMore = computed(() =>
  isSearching.value ? search.loadingMore : store.loadingMore,
)

const showLoading = computed(() =>
  loadingAny.value && resultCount.value === 0,
)

const showEmpty = computed(() =>
  !loadingAny.value && resultCount.value === 0,
)

const showSentinel = computed(() =>
  !showLoading.value && !showEmpty.value && (hasMore.value || loadingMore.value),
)

const displayError = computed(() =>
  isSearching.value ? search.error : store.error,
)

let observer: IntersectionObserver | null = null

async function openDoc(id: number) {
  const doc = await getDocument(id)
  ui.openDetail(doc)
}

function runSearch() {
  if (!search.query.trim()) {
    search.reset()
    void store.load()
    return
  }
  void search.runWithBrowseFilters(store)
}

function setMode(m: typeof modes[number]) {
  search.mode = m
  if (isSearching.value) void search.runWithBrowseFilters(store)
}

function toggleIncludeImages() {
  search.includeImgs = !search.includeImgs
  if (isSearching.value) void search.runWithBrowseFilters(store)
}

function loadMore() {
  if (isSearching.value) {
    void search.runWithBrowseFilters(store, true)
  } else {
    void store.loadMore()
  }
}

watch(
  () => search.query,
  (q, prev) => {
    if (!q.trim() && prev?.trim()) {
      search.reset()
      void store.load()
    }
  },
)

onMounted(async () => {
  await Promise.all([store.loadTags(), store.load()])
  observer = new IntersectionObserver(
    entries => {
      if (
        entries.some(e => e.isIntersecting)
        && hasMore.value
        && !loadingMore.value
        && !loadingAny.value
      ) {
        loadMore()
      }
    },
    { rootMargin: '200px' },
  )
  if (sentinel.value) observer.observe(sentinel.value)
})

onUnmounted(() => observer?.disconnect())
</script>

<style scoped>
.browse-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 4px;
}
.view-toggle {
  display: flex;
  border: 0.5px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
  flex-shrink: 0;
}
.view-toggle-btn {
  padding: 6px 12px;
  font-size: 12px;
  border: none;
  background: var(--surface);
  color: var(--muted);
  cursor: pointer;
}
.view-toggle-btn + .view-toggle-btn {
  border-left: 0.5px solid var(--border);
}
.view-toggle-btn.active {
  background: #f0ede8;
  color: var(--text);
  font-weight: 500;
}
.view-toggle-btn:hover:not(.active) {
  background: #faf9f7;
}
.doc-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 10px;
}
.doc-lines {
  display: flex;
  flex-direction: column;
}
.browse-loading {
  font-size: 13px;
  color: var(--hint);
  padding: 24px 0;
}
.browse-empty {
  padding: 24px 0;
  font-size: 13px;
}
.browse-sentinel {
  display: flex;
  justify-content: center;
  padding: 20px 0 8px;
  min-height: 48px;
}
.browse-load-more:disabled {
  opacity: .6;
  cursor: not-allowed;
}
</style>
