<template>
  <div>
    <h1 class="page-title">Tag browser</h1>
    <p class="page-sub">Unified view of source and overlay tags</p>

    <div class="filter-row mb-3">
      <input v-model="q" placeholder="Filter tags…" class="filter-input" @input="load" />
      <span v-for="o in origins" :key="o" class="chip" :class="{ active: origin === o }"
            @click="origin = o; load()">{{ o }}</span>
    </div>

    <div class="table-wrap">
      <table class="tag-table">
        <thead><tr><th>Tag</th><th>Origin</th><th>Sources</th><th>Frequency</th><th class="right">Docs</th><th></th></tr></thead>
        <tbody>
          <tr v-for="t in tags" :key="t.tag + t.origin">
            <td><span class="tag-pill" :class="`tag-pill--${t.origin}`">{{ t.tag }}</span></td>
            <td><span class="badge" :class="`badge-${t.origin}`">{{ t.origin }}</span></td>
            <td class="hint">—</td>
            <td><div class="freq-bar"><div class="freq-fill" :style="{ width: (t.count / maxCount * 100) + '%' }" /></div></td>
            <td class="right">{{ t.count }}</td>
            <td class="right">
              <button
                v-if="t.origin === 'source'"
                type="button"
                class="btn btn-sm"
                @click="openTrainFromSource(t.tag)"
              >Train classifier…</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <h2 class="section-title">Tag training sessions</h2>
    <p class="page-sub mb-2">Resume labeling or review accepted models (auto-tag new documents).</p>
    <div v-if="!sessions.length" class="hint mb-3">No training sessions yet.</div>
    <div v-else class="table-wrap mb-3">
      <table class="tag-table">
        <thead>
          <tr><th>Tag</th><th>Status</th><th>Labels</th><th class="right">Action</th></tr>
        </thead>
        <tbody>
          <tr v-for="s in sessions" :key="s.session_id">
            <td><span class="tag-pill tag-pill--learned">{{ s.tag }}</span></td>
            <td><span class="badge" :class="`badge-${s.status}`">{{ s.status }}</span></td>
            <td class="hint">+{{ s.positive_count }} / −{{ s.negative_count }}</td>
            <td class="right">
              <RouterLink
                :to="`/tags/train/${s.session_id}`"
                class="btn btn-sm"
              >
                {{ s.status === 'labeling' ? 'Continue' : 'Open' }}
              </RouterLink>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <TrainTagPrompt
      v-model:open="trainDialogOpen"
      :hint="trainDialogHint"
      :default-tag="pendingSourceTag"
      :busy="trainBusy"
      @confirm="onTrainFromSourceConfirm"
    />
  </div>
</template>
<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import {
  createTagTrainingFromSourceTag,
  listTagTrainingSessions,
  listTags,
  type TagRow,
  type TagTrainingSession,
} from '@/api/client'
import TrainTagPrompt from '@/components/TrainTagPrompt.vue'
import { useToastStore } from '@/stores/toast'

const tags     = ref<TagRow[]>([])
const sessions = ref<TagTrainingSession[]>([])
const q      = ref('')
const origin = ref('all')
const origins = ['all','source','inferred','manual','learned']
const maxCount = computed(() => Math.max(1, ...tags.value.map(t => t.count)))
const router = useRouter()
const toast = useToastStore()

const trainDialogOpen = ref(false)
const trainBusy = ref(false)
const pendingSourceTag = ref('')

const trainDialogHint = computed(() => {
  if (!pendingSourceTag.value) return ''
  return `All documents with source tag “${pendingSourceTag.value}” become positive examples.`
})

async function load() {
  const [tagRows, sessionRows] = await Promise.all([
    listTags({ q: q.value || undefined, origin: origin.value === 'all' ? undefined : origin.value }),
    listTagTrainingSessions(),
  ])
  tags.value = tagRows
  sessions.value = sessionRows.filter(s => s.status !== 'archived')
}

function openTrainFromSource(sourceTag: string) {
  pendingSourceTag.value = sourceTag
  trainDialogOpen.value = true
}

async function onTrainFromSourceConfirm(targetTag: string) {
  const sourceTag = pendingSourceTag.value
  if (!sourceTag) return
  trainBusy.value = true
  try {
    const session = await createTagTrainingFromSourceTag(sourceTag, targetTag)
    trainDialogOpen.value = false
    await router.push(`/tags/train/${session.session_id}`)
  } catch (e: any) {
    toast.push(e.message, 'error')
  } finally {
    trainBusy.value = false
  }
}

onMounted(load)
</script>
<style scoped>
.section-title { font-size: 15px; font-weight: 600; margin-top: 28px; margin-bottom: 6px }
.mb-2 { margin-bottom: 8px }
.mb-3 { margin-bottom: 16px }
.btn-sm { font-size: 11px; padding: 4px 8px; text-decoration: none; display: inline-block }
.badge-labeling { background: #FAEEDA; color: #854F0B }
.badge-accepted { background: #E8F4E1; color: #2D5A1E }
</style>
