<template>
  <div>
    <h1 class="page-title">Browse</h1>
    <p class="page-sub">All documents in your archive</p>

    <div class="filter-row">
      <span
        v-for="src in INGESTION_SOURCES"
        :key="src"
        class="chip"
        :class="{ active: store.sources.includes(src) }"
        @click="store.toggleSource(src)"
      >{{ SOURCE_LABELS[src] }}</span>
    </div>

    <p class="results-meta">
      <template v-if="store.loading">Loading…</template>
      <template v-else>{{ store.total.toLocaleString() }} documents</template>
    </p>

    <div v-if="store.loading && !store.documents.length" class="browse-loading">
      Loading documents…
    </div>

    <div v-else class="doc-grid">
      <DocGridCard
        v-for="doc in store.documents"
        :key="doc.id"
        :doc="doc"
        :selected="ui.activeDocId === doc.id"
        @click="openDoc(doc.id)"
      />
    </div>

    <div ref="sentinel" class="browse-sentinel">
      <button
        v-if="hasMore && !store.loading"
        class="btn browse-load-more"
        :disabled="store.loadingMore"
        @click="store.loadMore()"
      >
        {{ store.loadingMore ? 'Loading…' : 'Load more' }}
      </button>
    </div>

    <p v-if="store.error" class="error">{{ store.error }}</p>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { INGESTION_SOURCES, SOURCE_LABELS } from '@/constants/sources'
import { getDocument } from '@/api/client'
import { useBrowseStore } from '@/stores/browse'
import { useUiStore } from '@/stores/ui'
import DocGridCard from '@/components/DocGridCard.vue'

const store = useBrowseStore()
const ui = useUiStore()
const sentinel = ref<HTMLElement | null>(null)

const hasMore = computed(() => store.documents.length < store.total)

let observer: IntersectionObserver | null = null

async function openDoc(id: number) {
  const doc = await getDocument(id)
  ui.openDetail(doc)
}

onMounted(() => {
  store.load()
  observer = new IntersectionObserver(
    entries => {
      if (
        entries.some(e => e.isIntersecting)
        && store.documents.length < store.total
        && !store.loadingMore
        && !store.loading
      ) {
        store.loadMore()
      }
    },
    { rootMargin: '200px' },
  )
  if (sentinel.value) observer.observe(sentinel.value)
})

onUnmounted(() => observer?.disconnect())
</script>

<style scoped>
.doc-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 10px;
}
.browse-loading {
  font-size: 13px;
  color: var(--hint);
  padding: 24px 0;
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
