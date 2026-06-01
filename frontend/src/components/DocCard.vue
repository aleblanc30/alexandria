<template>
  <div class="card" :class="{ selected }" @click="$emit('click')">
    <div class="title">{{ doc.title || 'Untitled' }}</div>
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
</template>
<script setup lang="ts">
import { computed } from 'vue'
import type { DocumentListItem, DocumentOut } from '@/api/client'
import { clusterLabel, docSimilarity, docYear } from '@/lib/docDisplay'
import SourceBadge from './SourceBadge.vue'

const props = defineProps<{ doc: DocumentOut | DocumentListItem; selected?: boolean }>()
defineEmits(['click'])

const cluster = computed(() => clusterLabel(props.doc))
const year = computed(() => docYear(props.doc))
const similarity = computed(() => docSimilarity(props.doc))
</script>
<style scoped>
.card        { background: var(--surface); border: 0.5px solid var(--border); border-radius: var(--radius-lg); padding: 12px 16px; cursor: pointer; transition: border-color .1s; margin-bottom: 8px }
.card:hover  { border-color: rgba(0,0,0,.22) }
.card.selected { border-color: #85B7EB; background: #F4F9FE }
.title       { font-size: 13px; font-weight: 500; margin-bottom: 5px }
.meta        { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 11px }
.hint        { color: var(--hint) }
.tags        { flex: 1 }
.sim         { margin-left: auto; font-size: 10px; font-weight: 500 }
.badge--cluster { background: #EEEDFE; color: #3C3489; padding: 2px 7px; border-radius: 10px; font-size: 10px; font-weight: 500 }
.score-bar   { height: 3px; background: #E6F1FB; border-radius: 2px; margin-top: 6px }
.score-fill  { height: 100%; background: #378ADD; border-radius: 2px; transition: width .2s }
</style>
