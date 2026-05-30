<template>
  <div>
    <h1 class="page-title">Tag browser</h1>
    <p class="page-sub">Unified view of source and overlay tags</p>

    <div class="filter-row mb-3">
      <input v-model="q" placeholder="Filter tags…" class="filter-input" @input="load" />
      <span v-for="o in origins" :key="o" class="chip" :class="{ active: origin === o }"
            @click="origin = o; load()">{{ o }}</span>
    </div>

    <div class="table-wrap">
      <table class="tag-table">
        <thead><tr><th>Tag</th><th>Origin</th><th>Sources</th><th>Frequency</th><th class="right">Docs</th></tr></thead>
        <tbody>
          <tr v-for="t in tags" :key="t.tag + t.origin">
            <td><span class="tag-pill" :class="`tag-pill--${t.origin}`">{{ t.tag }}</span></td>
            <td><span class="badge" :class="`badge-${t.origin}`">{{ t.origin }}</span></td>
            <td class="hint">—</td>
            <td><div class="freq-bar"><div class="freq-fill" :style="{ width: (t.count / maxCount * 100) + '%' }" /></div></td>
            <td class="right">{{ t.count }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { listTags, type TagRow } from '@/api/client'

const tags   = ref<TagRow[]>([])
const q      = ref('')
const origin = ref('all')
const origins = ['all','source','inferred','manual']
const maxCount = computed(() => Math.max(1, ...tags.value.map(t => t.count)))

async function load() {
  tags.value = await listTags({ q: q.value || undefined, origin: origin.value === 'all' ? undefined : origin.value })
}
onMounted(load)
</script>
