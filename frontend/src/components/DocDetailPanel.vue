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
      <template v-if="doc.image">
        <section v-if="doc.image.image_type" class="section">
          <div class="section-label">Image type</div>
          <p class="panel-summary">{{ doc.image.image_type }}</p>
        </section>

        <section v-if="doc.description" class="section">
          <div class="section-label">Description</div>
          <p class="panel-summary">{{ doc.description }}</p>
        </section>

        <section v-if="doc.image.ocr_text" class="section">
          <div class="section-label">OCR text</div>
          <p class="panel-summary panel-ocr">{{ doc.image.ocr_text }}</p>
        </section>
      </template>

      <section v-else-if="doc.description" class="section">
        <div class="section-label">Summary</div>
        <p class="panel-summary">{{ doc.description }}</p>
      </section>

      <section v-if="doc.note" class="section">
        <div class="section-label">Notes</div>
        <p class="panel-summary panel-note">{{ doc.note }}</p>
      </section>

      <section class="section">
        <div class="section-label">Metadata</div>
        <div class="meta-grid">
          <span class="mkey">Source</span><span>{{ doc.source }}</span>
          <span class="mkey">Added</span><span>{{ doc.date_added ? new Date(doc.date_added*1000).toLocaleDateString() : '—' }}</span>
          <span class="mkey">Fetch</span><span>{{ doc.fetch_status }}</span>
          <span class="mkey">Chunks</span><span>{{ doc.chunks_count }}</span>
        </div>
        <div
          v-if="showWebLink || showZotero"
          class="link-row"
          :class="{ 'link-row--end': !showWebLink }"
        >
          <span v-if="showWebLink" class="link-url" :title="openUrl!">{{ openUrl }}</span>
          <button
            v-if="showWebLink"
            class="btn-xs"
            type="button"
            @click.stop="openLink()"
          >
            Open in new tab
          </button>
          <ZoteroOpenButton
            v-if="showZotero"
            :source-id="doc.source_id"
            :attachment-key="doc.zotero_attachment_key"
            size="panel"
          />
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

      <section v-if="coverUrl && !coverFailed" class="section section--cover">
        <div class="section-label">Cover</div>
        <img class="panel-cover" :src="coverUrl" alt="" @error="coverFailed = true" />
      </section>
    </div>
  </aside>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useUiStore } from '@/stores/ui'
import { getDocument, patchTags } from '@/api/client'
import type { DocumentDetail } from '@/api/client'
import { useDocLinks } from '@/composables/useDocLinks'
import { docCoverUrl } from '@/lib/docDisplay'
import SourceBadge from './SourceBadge.vue'
import ZoteroOpenButton from './ZoteroOpenButton.vue'

const route = useRoute()
const ui  = useUiStore()
const doc = ref<DocumentDetail | null>(null)
const newTag = ref('')

const coverUrl = computed(() => (doc.value ? docCoverUrl(doc.value) : null))
const coverFailed = ref(false)

const { openUrl, hasWebLink, canZotero, openLink } = useDocLinks(() => doc.value)

const isBrowse = computed(
  () => route.path === '/browse' || route.path.startsWith('/browse/'),
)
const showWebLink = computed(() => isBrowse.value && hasWebLink.value)
const showZotero = computed(() => isBrowse.value && canZotero.value)

watch(() => ui.activeDocId, async (id) => {
  coverFailed.value = false
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
.section--cover { text-align: center }
.panel-cover { width: 100%; max-width: 240px; height: auto; border-radius: var(--radius); border: 0.5px solid var(--border); box-shadow: 0 2px 12px rgba(0,0,0,.12) }
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
.link-row    { display: flex; align-items: center; gap: 8px; margin-top: 8px }
.link-url    { flex: 1; min-width: 0; font-size: 11px; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis }
.link-row--end { justify-content: flex-end }
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
.panel-summary { font-size: 13px; color: var(--muted); line-height: 1.45; margin: 0 }
.panel-note    { white-space: pre-line }
.panel-ocr     { white-space: pre-line; font-size: 12px; max-height: 220px; overflow-y: auto; background: #f5f4f0; border-radius: var(--radius); padding: 10px }
.score-bar   { height: 6px; background: #E6F1FB; border-radius: 3px; overflow: hidden; margin-bottom: 4px }
.score-fill  { height: 100%; background: #378ADD; border-radius: 3px }
</style>
