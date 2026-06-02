<template>
  <div v-if="open" class="train-prompt-backdrop" @click.self="cancel">
    <div class="train-prompt-panel" role="dialog" aria-modal="true" aria-labelledby="train-prompt-title">
      <h2 id="train-prompt-title" class="train-prompt-title">{{ title }}</h2>
      <p v-if="hint" class="hint train-prompt-hint">{{ hint }}</p>
      <label class="train-prompt-label" for="train-prompt-input">Target tag name</label>
      <input
        id="train-prompt-input"
        ref="inputRef"
        v-model="tagName"
        class="filter-input train-prompt-input"
        placeholder="e.g. systems-research"
        :disabled="busy"
        @keyup.enter="confirm"
      />
      <div class="train-prompt-actions">
        <button type="button" class="btn" :disabled="busy" @click="cancel">Cancel</button>
        <button
          type="button"
          class="btn btn-primary"
          :disabled="!tagName.trim() || busy"
          @click="confirm"
        >
          {{ busy ? 'Starting…' : confirmLabel }}
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'

const props = withDefaults(defineProps<{
  open: boolean
  title?: string
  hint?: string
  defaultTag?: string
  confirmLabel?: string
  busy?: boolean
}>(), {
  title: 'Train tag classifier',
  confirmLabel: 'Start training',
  busy: false,
})

const emit = defineEmits<{
  'update:open': [value: boolean]
  confirm: [tag: string]
}>()

const tagName = ref('')
const inputRef = ref<HTMLInputElement | null>(null)

watch(
  () => props.open,
  async (isOpen) => {
    if (!isOpen) return
    tagName.value = props.defaultTag ?? ''
    await nextTick()
    inputRef.value?.focus()
    inputRef.value?.select()
  },
)

function cancel() {
  if (props.busy) return
  emit('update:open', false)
}

function confirm() {
  const tag = tagName.value.trim()
  if (!tag || props.busy) return
  emit('confirm', tag)
}
</script>

<style scoped>
.train-prompt-backdrop {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, .25);
  z-index: 100;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}
.train-prompt-panel {
  width: 100%;
  max-width: 400px;
  background: var(--surface);
  border: 0.5px solid var(--border);
  border-radius: var(--radius-lg);
  padding: 20px;
  box-shadow: 0 8px 32px rgba(0, 0, 0, .12);
}
.train-prompt-title {
  font-size: 16px;
  font-weight: 600;
  margin-bottom: 8px;
}
.train-prompt-hint {
  margin-bottom: 12px;
  font-size: 13px;
  line-height: 1.4;
}
.train-prompt-label {
  display: block;
  font-size: 12px;
  color: var(--muted);
  margin-bottom: 6px;
}
.train-prompt-input {
  width: 100%;
  margin-bottom: 16px;
}
.train-prompt-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
.btn-primary {
  background: #378ADD;
  color: #fff;
  border-color: #378ADD;
}
.btn-primary:hover:not(:disabled) {
  background: #185FA5;
}
.btn-primary:disabled {
  opacity: .5;
  cursor: not-allowed;
}
</style>
