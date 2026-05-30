<template>
  <div>
    <h1 class="page-title">Search</h1>
    <p class="page-sub">Full-text and semantic search across all sources</p>

    <div class="search-bar">
      <input v-model="store.query" placeholder="Search by keyword or meaning…"
             @keyup.enter="store.run()" />
      <button @click="store.run()">Search</button>
    </div>

    <div class="filter-row">
      <span v-for="m in modes" :key="m" class="chip" :class="{ active: store.mode === m }"
            @click="store.mode = m as any; store.run()">{{ m }}</span>
      <span class="chip-sep" />
      <span v-for="s in sources" :key="s" class="chip"
            :class="{ active: store.sources.includes(s) }" @click="toggleSource(s)">{{ s }}</span>
      <span class="chip-sep" />
      <span class="chip" :class="{ active: store.includeImgs }"
            @click="store.includeImgs = !store.includeImgs; store.run()">+images</span>
    </div>

    <p class="results-meta">
      <template v-if="store.loading">Searching…</template>
      <template v-else>{{ store.total }} results</template>
    </p>

    <DocCard v-for="doc in store.results" :key="doc.id" :doc="doc"
             :selected="ui.activeDocId === doc.id"
             @click="openDoc(doc.id)" />

    <p v-if="store.error" class="error">{{ store.error }}</p>
  </div>
</template>
<script setup lang="ts">
import { useSearchStore } from '@/stores/search'
import { useUiStore }     from '@/stores/ui'
import { getDocument }    from '@/api/client'
import DocCard from '@/components/DocCard.vue'

const store   = useSearchStore()
const ui      = useUiStore()
const modes   = ['semantic','fulltext','hybrid']
const sources = ['zotero','firefox','calibre']

function toggleSource(s: string) {
  const idx = store.sources.indexOf(s)
  idx === -1 ? store.sources.push(s) : store.sources.splice(idx, 1)
  store.run()
}
async function openDoc(id: number) {
  const doc = await getDocument(id)
  ui.openDetail(doc)
}
</script>
