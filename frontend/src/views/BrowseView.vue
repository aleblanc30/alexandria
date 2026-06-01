<template>
  <div class="browse-layout">
    <BrowseNavPanel />

    <div class="browse-content">
      <h1 class="page-title">Browse</h1>
      <p class="page-sub">All documents in your archive</p>

      <p class="results-meta">
        <template v-if="store.loading">Loading…</template>
        <template v-else>
          {{ store.total.toLocaleString() }} document{{ store.total === 1 ? '' : 's' }}
          <span v-if="store.waybackOnly" class="hint"> · Wayback archive</span>
        </template>
      </p>

      <div v-if="store.loading && !store.documents.length" class="browse-loading">
        Loading documents…
      </div>

      <p
        v-else-if="!store.loading && store.documents.length === 0"
        class="browse-empty hint"
      >
        No documents match the current filters.
      </p>

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
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { getDocument } from '@/api/client'
import { useBrowseStore } from '@/stores/browse'
import { useUiStore } from '@/stores/ui'
import BrowseNavPanel from '@/components/BrowseNavPanel.vue'
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

onMounted(async () => {
  await Promise.all([store.loadTags(), store.load()])
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
