<template>
  <div class="toast-container" v-if="toast.messages.length">
    <div
      v-for="m in toast.messages"
      :key="m.id"
      class="toast"
      :class="`toast--${m.level}`"
      @click="toast.dismiss(m.id)"
    >
      {{ m.text }}
    </div>
  </div>
</template>

<script setup lang="ts">
import { useToastStore } from '@/stores/toast'
const toast = useToastStore()
</script>

<style scoped>
.toast-container {
  position: fixed;
  bottom: 24px;
  right: 24px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  z-index: 1000;
  max-width: 360px;
}
.toast {
  padding: 10px 14px;
  border-radius: var(--radius);
  font-size: 13px;
  background: var(--surface);
  border: 0.5px solid var(--border);
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.08);
  cursor: pointer;
  line-height: 1.4;
}
.toast--info  { color: var(--text) }
.toast--error { color: #b03a2e; border-left: 3px solid #b03a2e }
</style>
