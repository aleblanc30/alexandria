<template>
  <div class="card" :class="{ selected, 'card--pick': pickMode }" @click="$emit('click')">
    <label v-if="pickMode" class="card-check" @click.stop>
      <input type="checkbox" :checked="checked" @change="$emit('toggle-check')" />
    </label>
    <img
      v-if="coverUrl && !coverFailed"
      class="card-cover"
      :src="coverUrl"
      alt=""
      loading="lazy"
      @error="coverFailed = true"
    />
    <div class="card-body">
    <div class="title">{{ doc.title || 'Untitled' }}</div>
    <div v-if="description" class="desc">{{ description }}</div>
    <div class="meta">
      <SourceBadge :source="doc.source" />
      <span v-if="cluster" class="badge badge--cluster">{{ cluster }}</span>
      <span v-if="year" class="hint">{{ year }}</span>
      <span class="hint tags">{{ doc.source_tags.slice(0,3).map(t => '#' + t).join(' ') }}</span>
      <span v-if="similarity != null" class="sim">{{ Math.round(similarity * 100) }}%</span>
    </div>
    <div v-if="similarity != null" class="score-bar">
      <div class="score-fill" :style="{ width: similarity * 100 + '%' }" />
    </div>
    </div>
  </div>
</template>
<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { DocumentListItem, DocumentOut } from '@/api/client'
import { clusterLabel, docAddedYear, docCoverUrl, docDescription, docSimilarity } from '@/lib/docDisplay'
import SourceBadge from './SourceBadge.vue'

const props = defineProps<{
  doc: DocumentOut | DocumentListItem
  selected?: boolean
  pickMode?: boolean
  checked?: boolean
}>()
defineEmits<{ click: []; 'toggle-check': [] }>()

const cluster = computed(() => clusterLabel(props.doc))
const year = computed(() => docAddedYear(props.doc))
const similarity = computed(() => docSimilarity(props.doc))
const description = computed(() => docDescription(props.doc))
const coverUrl = computed(() => docCoverUrl(props.doc))
const coverFailed = ref(false)
watch(coverUrl, () => { coverFailed.value = false })
</script>
<style scoped>
.card        { background: var(--surface); border: 0.5px solid var(--border); border-radius: var(--radius-lg); padding: 12px 16px; cursor: pointer; transition: border-color .1s; margin-bottom: 8px; display: flex; gap: 10px; align-items: flex-start }
.card--pick  { padding-left: 10px }
.card-check  { flex-shrink: 0; padding-top: 2px; cursor: pointer }
.card-cover  { flex-shrink: 0; width: 34px; height: 50px; object-fit: cover; border-radius: var(--radius); border: 0.5px solid var(--border) }
.card-body   { flex: 1; min-width: 0 }
.card:hover  { border-color: rgba(0,0,0,.22) }
.card.selected { border-color: #85B7EB; background: #F4F9FE }
.title       { font-size: 13px; font-weight: 500; margin-bottom: 5px }
.desc        { font-size: 11px; color: var(--muted); line-height: 1.45; margin-bottom: 6px; display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden }
.meta        { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 11px }
.hint        { color: var(--hint) }
.tags        { flex: 1 }
.sim         { margin-left: auto; font-size: 10px; font-weight: 500 }
.badge--cluster { background: #EEEDFE; color: #3C3489; padding: 2px 7px; border-radius: 10px; font-size: 10px; font-weight: 500 }
.score-bar   { height: 3px; background: #E6F1FB; border-radius: 2px; margin-top: 6px }
.score-fill  { height: 100%; background: #378ADD; border-radius: 2px; transition: width .2s }
</style>
