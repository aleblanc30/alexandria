<template>
  <button
    :class="['zotero-open', size === 'panel' ? 'zotero-open--panel' : 'zotero-open--card']"
    type="button"
    title="Open in Zotero"
    aria-label="Open in Zotero"
    @click.stop="onClick"
  >
    <span class="zotero-mark" aria-hidden="true">Z</span>
    <span v-if="size === 'panel'" class="zotero-open-label">Open in Zotero</span>
  </button>
</template>

<script setup lang="ts">
import { openInZotero, zoteroOpenUri } from '@/lib/zotero'

const props = withDefaults(
  defineProps<{
    sourceId: string
    attachmentKey?: string | null
    size?: 'card' | 'panel'
  }>(),
  { size: 'card' },
)

function onClick() {
  openInZotero(zoteroOpenUri(props.sourceId, props.attachmentKey))
}
</script>

<style scoped>
.zotero-open {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 0;
  border: none;
  border-radius: var(--radius);
  background: transparent;
  cursor: pointer;
}
.zotero-open--card {
  width: 24px;
  height: 24px;
}
.zotero-open--panel {
  padding: 0;
  font-size: 11px;
  border: none;
  background: transparent;
}
.zotero-open:hover .zotero-mark {
  background: #e0ddd6;
}
.zotero-mark {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  border-radius: 5px;
  background: #ebe9e4;
  color: #CC2936;
  font-size: 12px;
  font-weight: 700;
  font-family: system-ui, -apple-system, sans-serif;
  line-height: 1;
  letter-spacing: -0.02em;
  transition: background 0.1s;
  flex-shrink: 0;
}
.zotero-open-label {
  line-height: 1;
  color: var(--muted);
}
.zotero-open--panel:hover .zotero-open-label {
  color: #185FA5;
}
</style>
