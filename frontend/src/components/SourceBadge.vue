<template>
  <span class="badge" :class="`badge--${source}`">{{ label }}</span>
</template>
<script setup lang="ts">
import { computed } from 'vue'
const props = defineProps<{ source: string }>()
const label = computed(() => ({ firefox: 'Firefox', zotero: 'Zotero', calibre: 'Calibre', image: 'Image' }[props.source] ?? props.source))
</script>
<style scoped>
.badge         { padding: 2px 7px; border-radius: 10px; font-size: 10px; font-weight: 500 }
.badge--firefox{ background: #E6F1FB; color: #185FA5 }
.badge--zotero { background: #EAF3DE; color: #3B6D11 }
.badge--calibre{ background: #FAEEDA; color: #854F0B }
.badge--image  { background: #EEEDFE; color: #3C3489 }
</style>
