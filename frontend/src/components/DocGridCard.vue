<template>
  <div class="grid-card" :class="{ selected }" @click="$emit('click')">
    <div class="grid-card-title">{{ doc.title || 'Untitled' }}</div>
    <div v-if="doc.description" class="grid-card-desc">{{ doc.description }}</div>
    <div class="grid-card-footer">
      <SourceBadge :source="doc.source" />
    </div>
  </div>
</template>

<script setup lang="ts">
import type { DocumentListItem } from '@/api/client'
import SourceBadge from './SourceBadge.vue'

defineProps<{ doc: DocumentListItem; selected?: boolean }>()
defineEmits<{ click: [] }>()
</script>

<style scoped>
.grid-card {
  display: flex;
  flex-direction: column;
  gap: 6px;
  background: var(--surface);
  border: 0.5px solid var(--border);
  border-radius: var(--radius-lg);
  padding: 12px;
  cursor: pointer;
  min-height: 100px;
  transition: border-color .1s;
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
.grid-card-footer { margin-top: auto }
</style>
