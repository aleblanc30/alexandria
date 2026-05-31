<template>
  <div v-if="source">
    <h1 class="page-title">{{ SOURCE_LABELS[source] }}</h1>
    <p class="page-sub">Sync metadata, run ingestion, and monitor progress</p>

    <IngestionSourcePanel :source="source" />

    <template v-if="source === 'firefox'">
      <h2 class="section-title mt-4">
        Unfetchable bookmarks <span class="hint">({{ ingest.unfetchable.length }})</span>
      </h2>
      <div class="table-wrap">
        <div v-for="u in ingest.unfetchable.slice(0, 50)" :key="u.id" class="unfetch-row">
          <span class="unfetch-url">{{ u.url }}</span>
          <span class="err-badge">{{ u.http_status ?? 'timeout' }}</span>
          <span class="hint">{{ u.error }}</span>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import IngestionSourcePanel from '@/components/IngestionSourcePanel.vue'
import { SOURCE_LABELS, isIngestionSource, type IngestionSource } from '@/constants/sources'
import { useIngestionStore } from '@/stores/ingestion'

const route = useRoute()
const router = useRouter()
const ingest = useIngestionStore()

const source = computed((): IngestionSource | null => {
  const value = route.params.source as string
  return isIngestionSource(value) ? value : null
})

watch(source, (value) => {
  if (!value) router.replace('/ingestion')
}, { immediate: true })

onMounted(() => ingest.load())
</script>

<style scoped>
.mt-4 { margin-top: 20px }
</style>
