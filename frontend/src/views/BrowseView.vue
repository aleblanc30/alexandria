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
          <span v-if="imageResults.length" class="hint">
            · {{ imageResults.length }} image{{ imageResults.length === 1 ? '' : 's' }}</span>
          <span v-if="store.waybackOnly" class="hint"> · Wayback archive</span>
        </template>
      </p>

      <div v-if="showLoading" class="browse-loading">
        {{ isSearching ? 'Searching…' : 'Loading documents…' }}
      </div>

      <template v-else>
        <div v-if="imageResults.length" class="image-section">
          <h2 class="image-section-title">
            Images
            <span class="hint">{{ imageResults.length }}</span>
          </h2>
          <div class="image-grid">
            <a
              v-for="img in imageResults"
              :key="img.id"
              class="image-tile"
              :href="imageFileUrl(img.id)"
              target="_blank"
              rel="noopener"
              :title="img.description || img.filename"
            >
              <img
                class="image-tile-img"
                :src="imageFileUrl(img.id)"
                :alt="img.description || img.filename"
                loading="lazy"
              />
              <span v-if="img.similarity != null" class="image-tile-sim">
                {{ Math.round(img.similarity * 100) }}%
              </span>
            </a>
          </div>
        </div>

        <p v-if="showEmpty" class="browse-empty hint">
          {{ isSearching ? 'No results match your search and filters.' : 'No documents match the current filters.' }}
        </p>

        <div v-else-if="resultCount && store.viewMode === 'cards'" class="doc-grid">
          <DocGridCard
            v-for="doc in gridDocs"
            :key="doc.id"
            :doc="doc"
            :selected="ui.activeDocId === doc.id"
            pick-mode
            :checked="store.isDocSelected(doc.id)"
            @click="openDoc(doc.id)"
            @toggle-check="store.toggleDocSelection(doc.id)"
          />
        </div>

        <div v-else-if="resultCount" class="doc-lines">
        <DocCard
          v-for="doc in lineDocs"
          :key="doc.id"
          :doc="doc"
          :selected="ui.activeDocId === doc.id"
          pick-mode
          :checked="store.isDocSelected(doc.id)"
          @click="openDoc(doc.id)"
          @toggle-check="store.toggleDocSelection(doc.id)"
        />
        </div>
      </template>

      <div v-if="selectionCount > 0" class="browse-bulk-bar">
        <span>{{ selectionCount }} selected</span>
        <button type="button" class="btn" @click="selectAllVisible">Select page</button>
        <button type="button" class="btn" @click="store.clearSelection()">Clear</button>
        <button type="button" class="btn btn-primary" @click="startTrainingFromSelection">
          Train classifier…
        </button>
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

    <TrainTagPrompt
      v-model:open="trainDialogOpen"
      :hint="trainDialogHint"
      :busy="trainBusy"
      @confirm="onTrainConfirm"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { createTagTrainingSession, getDocument, imageFileUrl } from '@/api/client'
import type { DocumentListItem, DocumentOut, ImageOut } from '@/api/client'
import { toGridItem } from '@/lib/docDisplay'
import { useBrowseStore } from '@/stores/browse'
import { useSearchStore } from '@/stores/search'
import { useUiStore } from '@/stores/ui'
import { useToastStore } from '@/stores/toast'
import BrowseNavPanel from '@/components/BrowseNavPanel.vue'
import DocCard from '@/components/DocCard.vue'
import DocGridCard from '@/components/DocGridCard.vue'
import TrainTagPrompt from '@/components/TrainTagPrompt.vue'

const store = useBrowseStore()
const search = useSearchStore()
const ui = useUiStore()
const router = useRouter()
const toast = useToastStore()
const sentinel = ref<HTMLElement | null>(null)
const trainDialogOpen = ref(false)
const trainBusy = ref(false)

const modes = ['semantic', 'fulltext', 'hybrid'] as const

const isSearching = computed(() => search.query.trim().length > 0)

const gridDocs = computed<DocumentListItem[]>(() =>
  isSearching.value ? search.results.map(toGridItem) : store.documents,
)

const lineDocs = computed<(DocumentOut | DocumentListItem)[]>(() =>
  isSearching.value ? search.results : store.documents,
)

const resultCount = computed(() =>
  isSearching.value ? search.results.length : store.documents.length,
)

// Image-pipeline results come from search (with `+images` on) or, when just
// browsing, from the browse store (whenever the Images source is in scope).
const imageResults = computed<ImageOut[]>(() =>
  isSearching.value ? search.images : store.images,
)

const hasMore = computed(() =>
  isSearching.value
    ? search.results.length < search.total
    : store.documents.length < store.total || !store.imagesExhausted,
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
  !loadingAny.value && resultCount.value === 0 && imageResults.value.length === 0,
)

const showSentinel = computed(() =>
  !showLoading.value && !showEmpty.value && (hasMore.value || loadingMore.value),
)

const displayError = computed(() =>
  isSearching.value ? search.error : store.error,
)

const selectionCount = computed(() => store.selectedDocIds.size)

const trainDialogHint = computed(() => {
  const n = selectionCount.value
  return n === 1
    ? '1 document will be a positive training example.'
    : `${n} documents will be positive training examples.`
})

function visibleDocIds(): number[] {
  const docs = store.viewMode === 'cards' ? gridDocs.value : lineDocs.value
  return docs.map(d => d.id)
}

function selectAllVisible() {
  store.selectAllOnPage(visibleDocIds())
}

function startTrainingFromSelection() {
  if (selectionCount.value === 0) {
    toast.push('Select at least one document using the checkboxes.', 'error')
    return
  }
  trainDialogOpen.value = true
}

async function onTrainConfirm(tag: string) {
  const ids = Array.from(store.selectedDocIds)
  if (!ids.length) {
    toast.push('Selection is empty.', 'error')
    trainDialogOpen.value = false
    return
  }
  trainBusy.value = true
  try {
    const session = await createTagTrainingSession(
      tag,
      ids.map(doc_id => ({ doc_id, label: 1 })),
    )
    store.clearSelection()
    trainDialogOpen.value = false
    await router.push(`/tags/train/${session.session_id}`)
  } catch (e: any) {
    toast.push(e.message, 'error')
  } finally {
    trainBusy.value = false
  }
}

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
.image-section {
  margin-bottom: 16px;
}
.image-section-title {
  font-size: 13px;
  font-weight: 500;
  margin: 0 0 8px;
  display: flex;
  align-items: baseline;
  gap: 6px;
}
.image-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
  gap: 8px;
}
.image-tile {
  position: relative;
  display: block;
  aspect-ratio: 1;
  border-radius: var(--radius);
  overflow: hidden;
  border: 0.5px solid var(--border);
  background: var(--surface);
}
.image-tile-img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}
.image-tile-sim {
  position: absolute;
  bottom: 4px;
  right: 4px;
  padding: 1px 5px;
  border-radius: 8px;
  font-size: 10px;
  font-weight: 500;
  color: #fff;
  background: rgba(0, 0, 0, .6);
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
.browse-bulk-bar {
  position: sticky;
  bottom: 12px;
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  padding: 10px 14px;
  margin-top: 12px;
  background: var(--surface);
  border: 0.5px solid var(--border);
  border-radius: var(--radius-lg);
  box-shadow: 0 4px 16px rgba(0, 0, 0, .08);
  font-size: 13px;
  z-index: 20;
}
.btn-primary {
  margin-left: auto;
  background: #378ADD;
  color: #fff;
  border-color: #378ADD;
}
.btn-primary:hover {
  background: #185FA5;
}
</style>
