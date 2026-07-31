<template>
  <div class="grid-card" :class="{ selected, 'grid-card--pick': pickMode }" @click="$emit('click')">
    <label v-if="pickMode" class="grid-card-check" @click.stop>
      <input type="checkbox" :checked="checked" @change="$emit('toggle-check')" />
    </label>
    <img
      v-if="coverUrl && !coverFailed"
      class="grid-card-cover"
      :src="coverUrl"
      alt=""
      loading="lazy"
      @error="coverFailed = true"
    />
    <div class="grid-card-inner">
    <div class="grid-card-title">{{ doc.title || 'Untitled' }}</div>
    <div v-if="doc.description" class="grid-card-desc">{{ doc.description }}</div>
    <div v-if="hasTags" class="grid-card-tags">
      <span v-for="t in doc.source_tags" :key="'s-' + t" class="tag-pill tag-pill--source">#{{ t }}</span>
      <span v-for="t in doc.cluster_l1_tags" :key="'l1-' + t" class="tag-pill tag-pill--cluster_l1">{{ t }}</span>
      <span v-for="t in doc.cluster_l2_tags" :key="'l2-' + t" class="tag-pill tag-pill--cluster_l2">{{ t }}</span>
    </div>
    <div class="grid-card-footer">
      <SourceBadge :source="doc.source" />
      <div v-if="hasWebLink || showZotero" class="grid-card-actions">
        <button
          v-if="hasWebLink"
          class="grid-card-link"
          type="button"
          title="Open in new tab"
          aria-label="Open in new tab"
          @click.stop="openLink"
        >
          <svg
            class="grid-card-link-icon"
            viewBox="0 0 16 16"
            fill="none"
            stroke="currentColor"
            stroke-width="1.5"
            stroke-linecap="round"
            stroke-linejoin="round"
            aria-hidden="true"
          >
            <rect x="2.5" y="8.5" width="6" height="6" rx="1" />
            <line x1="8.5" y1="8.5" x2="13.5" y2="3.5" />
            <polyline points="9.5,3.5 13.5,3.5 13.5,7.5" />
          </svg>
        </button>
        <ZoteroOpenButton
          v-if="showZotero"
          :source-id="doc.source_id"
          :attachment-key="doc.zotero_attachment_key"
          size="card"
        />
      </div>
    </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { DocumentListItem } from '@/api/client'
import { useDocLinks } from '@/composables/useDocLinks'
import { docCoverUrl } from '@/lib/docDisplay'
import SourceBadge from './SourceBadge.vue'
import ZoteroOpenButton from './ZoteroOpenButton.vue'

const props = defineProps<{
  doc: DocumentListItem
  selected?: boolean
  pickMode?: boolean
  checked?: boolean
}>()
defineEmits<{ click: []; 'toggle-check': [] }>()

const { hasWebLink, canZotero: showZotero, openLink } = useDocLinks(() => props.doc)

const coverUrl = computed(() => docCoverUrl(props.doc))
const coverFailed = ref(false)
watch(coverUrl, () => { coverFailed.value = false })

const hasTags = computed(
  () =>
    props.doc.source_tags.length > 0
    || props.doc.cluster_l1_tags.length > 0
    || props.doc.cluster_l2_tags.length > 0,
)
</script>

<style scoped>
.grid-card {
  display: flex;
  flex-direction: row;
  gap: 8px;
  align-items: flex-start;
  background: var(--surface);
  border: 0.5px solid var(--border);
  border-radius: var(--radius-lg);
  padding: 12px;
  cursor: pointer;
  min-height: 100px;
  transition: border-color .1s;
}
.grid-card-inner {
  display: flex;
  flex-direction: column;
  gap: 6px;
  flex: 1;
  min-width: 0;
}
.grid-card-check { flex-shrink: 0; padding-top: 2px; cursor: pointer }
.grid-card-cover {
  flex-shrink: 0;
  width: 52px;
  height: 76px;
  object-fit: cover;
  border-radius: var(--radius);
  border: 0.5px solid var(--border);
  background: var(--surface);
}
.grid-card:hover { border-color: rgba(0, 0, 0, .22) }
.grid-card.selected { border-color: #85B7EB; background: #F4F9FE }
.grid-card-title {
  font-size: 13px;
  font-weight: 500;
  line-height: 1.35;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.grid-card-desc {
  flex: 1;
  font-size: 11px;
  color: var(--muted);
  line-height: 1.45;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.grid-card-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.grid-card-footer {
  margin-top: auto;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
}
.grid-card-actions {
  display: flex;
  align-items: center;
  gap: 4px;
  flex-shrink: 0;
}
.grid-card-link {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  padding: 0;
  border: none;
  border-radius: var(--radius);
  background: transparent;
  color: var(--hint);
  cursor: pointer;
}
.grid-card-link:hover {
  background: #f0ede8;
  color: #185FA5;
}
.grid-card-link-icon {
  display: block;
  width: 14px;
  height: 14px;
  flex-shrink: 0;
}
</style>
