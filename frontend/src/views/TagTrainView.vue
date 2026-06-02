<template>
  <div class="train-layout">
    <div class="train-main">
      <RouterLink to="/tags" class="hint train-back">← Tags</RouterLink>
      <h1 class="page-title">Train tag: {{ session?.tag ?? '…' }}</h1>
      <p v-if="session" class="page-sub">
        <span class="badge" :class="`badge-${session.status}`">{{ session.status }}</span>
        {{ session.positive_count }} positive · {{ session.negative_count }} negative
        <span v-if="seedSummary"> · {{ seedSummary }}</span>
        <span v-if="session.train_stats?.warn_small_seed" class="train-warn"> · small seed</span>
      </p>

      <div v-if="loading" class="hint">Loading…</div>
      <div v-else-if="error" class="error">{{ error }}</div>
      <template v-else-if="session">
        <div v-if="session.status === 'accepted'" class="train-done">
          <p>
            Model accepted. Tag
            <span class="tag-pill tag-pill--learned">#{{ session.tag }}</span>
            is applied automatically to new documents when they are embedded.
          </p>
          <button type="button" class="btn btn-primary" :disabled="resuming" @click="resume">
            {{ resuming ? 'Resuming…' : 'Resume training' }}
          </button>
        </div>

        <section v-if="session.status === 'labeling'" class="train-queue">
          <h2 class="section-title">Review uncertain documents</h2>
          <p v-if="!queue.length" class="hint">
            No unlabeled documents in queue. Accept the model or ingest more documents.
          </p>
          <div v-for="item in queue" :key="item.doc_id" class="queue-row">
            <div class="queue-info">
              <div class="queue-title">{{ item.title || `Document #${item.doc_id}` }}</div>
              <div class="hint">P(tag) {{ Math.round(item.probability * 100) }}%</div>
            </div>
            <div class="queue-actions">
              <button type="button" class="btn" @click="label(item.doc_id, 1)">Yes</button>
              <button type="button" class="btn" @click="label(item.doc_id, 0)">No</button>
              <button type="button" class="btn btn-ghost" @click="openDoc(item.doc_id)">Open</button>
            </div>
          </div>
        </section>

        <section v-if="session.status === 'labeling' && session.has_model" class="train-pseudo">
          <h2 class="section-title">Pseudo-labeling</h2>
          <p class="hint">
            Model: add labels when P(tag) ≥ 95% or ≤ 5%. LLM: tag + seed collection + your No
            labels (or random negatives), then one-shot on a random subset (batch {{ pseudoLlmBatch }}).
          </p>
          <p v-if="pseudoSummary" class="pseudo-summary">{{ pseudoSummary }}</p>
          <div class="train-actions train-actions--row">
            <button type="button" class="btn" :disabled="!!pseudoBusy" @click="runPseudo('model')">
              {{ pseudoBusy === 'model' ? 'Scoring…' : 'Pseudo-label (model)' }}
            </button>
            <button type="button" class="btn" :disabled="!!pseudoBusy" @click="runPseudo('llm')">
              {{ pseudoBusy === 'llm' ? 'LLM (may take a few minutes)…' : 'Pseudo-label (LLM)' }}
            </button>
          </div>
        </section>

        <div v-if="session.status === 'labeling'" class="train-actions">
          <button type="button" class="btn btn-primary" :disabled="accepting || !session.has_model" @click="accept">
            {{ accepting ? 'Applying…' : 'Accept model' }}
          </button>
        </div>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import {
  acceptTagTrainingSession,
  getDocument,
  getTagTrainingQueue,
  getTagTrainingSession,
  postTagTrainingLabels,
  pseudoLabelTagTrainingSession,
  resumeTagTrainingSession,
  type TagTrainingQueueDoc,
  type TagTrainingSession,
} from '@/api/client'
import { useToastStore } from '@/stores/toast'
import { useUiStore } from '@/stores/ui'

const route = useRoute()
const ui = useUiStore()
const toast = useToastStore()

const session = ref<TagTrainingSession | null>(null)
const queue = ref<TagTrainingQueueDoc[]>([])
const loading = ref(true)
const accepting = ref(false)
const resuming = ref(false)
const pseudoBusy = ref<'model' | 'llm' | null>(null)
const error = ref<string | null>(null)

const sessionId = computed(() => Number(route.params.sessionId))

const pseudoLlmBatch = computed(() => {
  const n = session.value?.parameters?.pseudo_llm_batch_size
  return typeof n === 'number' ? n : 20
})

const pseudoSummary = computed(() => {
  const r = session.value?.pseudo_label_result
  if (!r) return ''
  const parts = [`+${r.added_positive}`, `−${r.added_negative}`]
  if (r.mode === 'model' && r.pseudo_label_high != null) {
    parts.push(`thresholds ${Math.round((r.pseudo_label_low ?? 0.05) * 100)}% / ${Math.round(r.pseudo_label_high * 100)}%`)
  }
  if (r.errors) parts.push(`${r.errors} errors`)
  return `Last run (${r.mode}): ${parts.join(' · ')}`
})

const seedSummary = computed(() => {
  const p = session.value?.provenance
  if (!p) return ''
  if (typeof p.from_source_tag === 'string') {
    return `seeded from source tag “${p.from_source_tag}”`
  }
  return ''
})

async function loadQueue() {
  if (session.value?.status !== 'labeling') {
    queue.value = []
    return
  }
  queue.value = await getTagTrainingQueue(sessionId.value)
}

async function load() {
  loading.value = true
  error.value = null
  try {
    session.value = await getTagTrainingSession(sessionId.value)
    await loadQueue()
  } catch (e: any) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

async function label(docId: number, labelVal: number) {
  try {
    session.value = await postTagTrainingLabels(sessionId.value, [{ doc_id: docId, label: labelVal }])
    await loadQueue()
  } catch (e: any) {
    toast.push(e.message, 'error')
  }
}

async function runPseudo(mode: 'model' | 'llm') {
  pseudoBusy.value = mode
  try {
    session.value = await pseudoLabelTagTrainingSession(sessionId.value, mode)
    await loadQueue()
    const r = session.value.pseudo_label_result
    if (r) {
      toast.push(
        `Pseudo-label (${mode}): ${r.added_positive} positive, ${r.added_negative} negative`,
        'info',
      )
    }
  } catch (e: any) {
    toast.push(e.message, 'error')
  } finally {
    pseudoBusy.value = null
  }
}

async function accept() {
  accepting.value = true
  try {
    session.value = await acceptTagTrainingSession(sessionId.value)
    queue.value = []
    toast.push(`Tag “${session.value.tag}” applied to matching documents`, 'info')
  } catch (e: any) {
    toast.push(e.message, 'error')
  } finally {
    accepting.value = false
  }
}

async function resume() {
  resuming.value = true
  try {
    session.value = await resumeTagTrainingSession(sessionId.value)
    await loadQueue()
    toast.push('Training resumed — review the queue and accept again when ready', 'info')
  } catch (e: any) {
    toast.push(e.message, 'error')
  } finally {
    resuming.value = false
  }
}

async function openDoc(id: number) {
  const doc = await getDocument(id)
  ui.openDetail(doc)
}

watch(sessionId, () => { void load() })
onMounted(() => { void load() })
</script>

<style scoped>
.train-layout { max-width: 720px }
.train-back { display: inline-block; margin-bottom: 8px; text-decoration: none }
.train-warn { color: #854F0B }
.train-done {
  padding: 12px 14px;
  background: #E1F5EE;
  border-radius: var(--radius-lg);
  margin-bottom: 16px;
  font-size: 13px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  align-items: flex-start;
}
.section-title { font-size: 14px; font-weight: 600; margin: 20px 0 10px }
.queue-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 0;
  border-bottom: 0.5px solid var(--border);
}
.queue-info { flex: 1; min-width: 0 }
.queue-title { font-size: 13px; font-weight: 500; margin-bottom: 2px }
.queue-actions { display: flex; gap: 6px; flex-shrink: 0 }
.btn-ghost { background: transparent }
.train-pseudo { margin-top: 8px }
.pseudo-summary { font-size: 12px; color: var(--muted); margin: 8px 0 0 }
.train-actions { margin-top: 20px; display: flex; gap: 10px }
.train-actions--row { margin-top: 10px }
.btn-primary {
  background: #378ADD;
  color: #fff;
  border-color: #378ADD;
}
.btn-primary:hover:not(:disabled) { background: #185FA5 }
.btn-primary:disabled { opacity: .5; cursor: not-allowed }
.badge-labeling { background: #FAEEDA; color: #854F0B }
.badge-accepted { background: #E8F4E1; color: #2D5A1E }
.badge-archived { background: #f0ede8; color: var(--muted) }
</style>
