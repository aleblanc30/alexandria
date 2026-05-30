<template>
  <div class="overlay" @click.self="ui.closeDetail()" />
  <aside class="panel">
    <div class="panel-header">
      <div class="panel-title-wrap">
        <p class="panel-title">{{ doc?.title }}</p>
        <div class="panel-badges">
          <SourceBadge v-if="doc" :source="doc.source" />
          <span v-if="doc?.cluster_label" class="badge badge--cluster">{{ doc.cluster_label }}</span>
        </div>
      </div>
      <button class="close-btn" @click="ui.closeDetail()">✕</button>
    </div>

    <div v-if="doc" class="panel-body">
      <section class="section">
        <div class="section-label">Metadata</div>
        <div class="meta-grid">
          <span class="mkey">Source</span><span>{{ doc.source }}</span>
          <span class="mkey">Added</span><span>{{ doc.date_added ? new Date(doc.date_added*1000).toLocaleDateString() : '—' }}</span>
          <span class="mkey">Fetch</span><span>{{ doc.fetch_status }}</span>
          <span class="mkey">Chunks</span><span>{{ doc.chunks_count }}</span>
        </div>
      </section>

      <section class="section">
        <div class="section-label">Tags</div>
        <div class="tag-wrap">
          <span v-for="t in doc.source_tags" :key="t" class="tag tag--source">#{{ t }}</span>
          <span v-for="t in doc.overlay_tags" :key="t.tag" class="tag" :class="`tag--${t.origin}`">#{{ t.tag }}</span>
        </div>
        <div class="add-tag">
          <input v-model="newTag" placeholder="Add tag…" @keyup.enter="addTag" />
          <button @click="addTag">Add</button>
        </div>
      </section>

      <section class="section">
        <div class="section-label">Collections</div>
        <p class="hint-text">{{ doc.collections?.join(', ') || '—' }}</p>
      </section>

      <section v-if="doc.similarity != null" class="section">
        <div class="section-label">Similarity</div>
        <div class="score-bar"><div class="score-fill" :style="{ width: doc.similarity * 100 + '%' }" /></div>
        <span class="hint-text">{{ Math.round(doc.similarity * 100) }}%</span>
      </section>
    </div>
  </aside>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { useUiStore } from '@/stores/ui'
import { getDocument, patchTags } from '@/api/client'
import type { DocumentDetail } from '@/api/client'
import SourceBadge from './SourceBadge.vue'

const ui  = useUiStore()
const doc = ref<DocumentDetail | null>(null)
const newTag = ref('')

watch(() => ui.activeDocId, async (id) => {
  if (id == null) { doc.value = null; return }
  doc.value = await getDocument(id)
}, { immediate: true })

async function addTag() {
  const t = newTag.value.trim()
  if (!t || !doc.value) return
  await patchTags(doc.value.id, [t], [])
  doc.value = await getDocument(doc.value.id)
  newTag.value = ''
}
</script>

<style scoped>
.overlay   { position: fixed; inset: 0; background: rgba(0,0,0,.18); z-index: 10 }
.panel     { position: fixed; top: 0; right: 0; width: var(--panel-w); height: 100%; background: var(--surface); border-left: 0.5px solid var(--border); z-index: 11; display: flex; flex-direction: column; animation: slideIn .2s ease }
@keyframes slideIn { from { transform: translateX(100%) } to { transform: translateX(0) } }
.panel-header { padding: 16px; border-bottom: 0.5px solid var(--border); display: flex; gap: 10px; align-items: flex-start }
.panel-title-wrap { flex: 1; min-width: 0 }
.panel-title { font-size: 13px; font-weight: 500; line-height: 1.4; margin-bottom: 6px }
.panel-badges { display: flex; gap: 5px; flex-wrap: wrap }
.close-btn { border: none; background: none; cursor: pointer; color: var(--hint); font-size: 16px; padding: 2px 4px; border-radius: 4px }
.close-btn:hover { background: #f0ede8 }
.panel-body  { flex: 1; overflow-y: auto; padding: 16px }
.section     { margin-bottom: 18px }
.section-label { font-size: 10px; text-transform: uppercase; letter-spacing: .08em; color: var(--hint); font-weight: 500; margin-bottom: 8px }
.meta-grid   { display: grid; grid-template-columns: auto 1fr; gap: 4px 16px; font-size: 12px; background: #f5f4f0; border-radius: var(--radius); padding: 10px }
.mkey        { color: var(--muted) }
.tag-wrap    { display: flex; flex-wrap: wrap; gap: 5px }
.tag         { padding: 3px 9px; border-radius: 10px; font-size: 11px; cursor: pointer }
.tag--source   { background: #F1EFE8; color: #5F5E5A }
.tag--inferred { background: #FAEEDA; color: #854F0B }
.tag--manual   { background: #E1F5EE; color: #0F6E56 }
.tag--llm      { background: #EEEDFE; color: #3C3489 }
.badge--cluster { background: #EEEDFE; color: #3C3489; padding: 2px 7px; border-radius: 10px; font-size: 10px; font-weight: 500 }
.add-tag     { display: flex; gap: 6px; margin-top: 8px }
.add-tag input  { flex: 1; padding: 5px 8px; border: 0.5px solid var(--border); border-radius: var(--radius); font-size: 12px }
.add-tag button { padding: 4px 10px; font-size: 11px; border: 0.5px solid var(--border); border-radius: var(--radius); background: #f5f4f0; cursor: pointer }
.hint-text   { font-size: 12px; color: var(--muted) }
.score-bar   { height: 6px; background: #E6F1FB; border-radius: 3px; overflow: hidden; margin-bottom: 4px }
.score-fill  { height: 100%; background: #378ADD; border-radius: 3px }
</style>
