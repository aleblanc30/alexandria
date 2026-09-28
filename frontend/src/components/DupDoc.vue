<template>
  <div class="dup-doc">
    <SourceBadge :source="doc.source" />
    <span class="dup-doc-title" :title="doc.url_or_path ?? ''">{{ doc.title || doc.url_or_path || `#${doc.id}` }}</span>
  </div>
</template>

<script setup lang="ts">
import type { DuplicateLink } from '@/api/client'
import SourceBadge from './SourceBadge.vue'

defineProps<{ doc: DuplicateLink['canonical'] }>()
</script>

<style scoped>
.dup-doc { display: flex; align-items: center; gap: 6px; min-width: 0 }
.dup-doc-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 320px }
</style>
