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
            <td>
              <span
                class="tag-pill"
                :class="`tag-pill--${t.origin}`"
                :title="variantHint(t)"
              >{{ t.tag }}</span>
              <span v-if="(t.variants?.length ?? 0) > 1" class="hint variant-count">
                +{{ (t.variants?.length ?? 1) - 1 }} spelling{{ (t.variants?.length ?? 1) > 2 ? 's' : '' }}
              </span>
            </td>
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
              <button
                v-if="canDelete(t)"
                type="button"
                class="btn btn-sm btn-danger"
                :disabled="deleting === t.tag + t.origin"
                @click="removeTag(t)"
              >Delete</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <h2 class="section-title">Duplicate tags</h2>
    <p class="page-sub mb-2">
      Spellings that differ only in case, accents or punctuation are already one tag.
      Proposals below are equivalent tags found by the local embedding model, plurals and
      initialisms; nothing is folded until you accept it.
    </p>
    <div class="filter-row mb-2">
      <button type="button" class="btn btn-sm" :disabled="scanning" @click="runScan">
        {{ scanning ? 'Scanning…' : 'Find duplicates' }}
      </button>
      <span class="merge-form">
        <input v-model="mergeAlias" placeholder="Tag" class="filter-input merge-input" />
        <span class="hint">into</span>
        <input v-model="mergeCanonical" placeholder="Tag to keep" class="filter-input merge-input" />
        <button
          type="button"
          class="btn btn-sm"
          :disabled="!mergeAlias.trim() || !mergeCanonical.trim()"
          @click="manualMerge"
        >Merge</button>
      </span>
    </div>
    <div v-if="!candidates.length" class="hint mb-3">No proposals waiting.</div>
    <div v-else class="table-wrap mb-3">
      <table class="tag-table">
        <thead>
          <tr><th>Fold</th><th>Into</th><th>Why</th><th class="right">Action</th></tr>
        </thead>
        <tbody>
          <tr v-for="c in candidates" :key="c.id">
            <td>
              <span class="tag-pill tag-pill--source">{{ c.alias.label }}</span>
              <span class="hint"> {{ c.alias.documents }} docs</span>
              <div v-if="c.alias.examples?.length" class="hint examples">{{ c.alias.examples.join(' · ') }}</div>
            </td>
            <td>
              <span class="tag-pill tag-pill--source">{{ c.canonical.label }}</span>
              <span class="hint"> {{ c.canonical.documents }} docs</span>
              <div v-if="c.canonical.examples?.length" class="hint examples">{{ c.canonical.examples.join(' · ') }}</div>
            </td>
            <td class="hint">{{ c.kind }}<template v-if="c.score != null"> {{ c.score.toFixed(2) }}</template></td>
            <td class="right nowrap">
              <button type="button" class="btn btn-sm" @click="decide(c.id, true)">Merge</button>
              <button type="button" class="btn btn-sm" @click="decide(c.id, false)">Keep apart</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <details v-if="merged.length" class="mb-3">
      <summary class="hint">{{ merged.length }} merged tag{{ merged.length > 1 ? 's' : '' }}</summary>
      <table class="tag-table">
        <tbody>
          <tr v-for="m in merged" :key="m.id">
            <td><span class="tag-pill tag-pill--source">{{ m.alias.label }}</span></td>
            <td class="hint">→</td>
            <td><span class="tag-pill tag-pill--source">{{ m.canonical.label }}</span></td>
            <td class="right">
              <button type="button" class="btn btn-sm" @click="decide(m.id, false)">Unmerge</button>
            </td>
          </tr>
        </tbody>
      </table>
    </details>

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
  acceptTagAlias,
  createTagTrainingFromSourceTag,
  deleteTag,
  listTagAliases,
  listTagTrainingSessions,
  listTags,
  mergeTags,
  rejectTagAlias,
  scanTagAliases,
  type TagAlias,
  type TagRow,
  type TagTrainingSession,
} from '@/api/client'
import TrainTagPrompt from '@/components/TrainTagPrompt.vue'
import { useToastStore } from '@/stores/toast'
import { errorMessage } from '@/lib/notifyError'

const tags     = ref<TagRow[]>([])
const sessions = ref<TagTrainingSession[]>([])
const q      = ref('')
const origin = ref('all')
const origins = ['all','source','inferred','manual','learned']
// Origins the API lets a user delete: nothing regenerates them. Source tags are
// rewritten by each sync, and cluster and collection tags are derived.
const deletableOrigins = ['manual', 'inferred', 'llm', 'learned']
const deleting = ref('')
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

const candidates = ref<TagAlias[]>([])
const merged = ref<TagAlias[]>([])
const scanning = ref(false)
const mergeAlias = ref('')
const mergeCanonical = ref('')

function canDelete(t: TagRow): boolean {
  return deletableOrigins.includes(t.origin)
}

async function removeTag(t: TagRow) {
  const learnedNote = t.origin === 'learned'
    ? '\n\nIts trained model is archived too, so the tag is not applied to new documents.'
    : ''
  const ok = window.confirm(
    `Delete the ${t.origin} tag “${t.tag}” from ${t.count} document${t.count === 1 ? '' : 's'}?${learnedNote}`,
  )
  if (!ok) return
  deleting.value = t.tag + t.origin
  try {
    const res = await deleteTag(t.tag, t.origin)
    toast.push(`Deleted “${t.tag}” from ${res.documents} document${res.documents === 1 ? '' : 's'}`, 'info')
    await refreshAll()
  } catch (e: unknown) {
    toast.push(errorMessage(e), 'error')
  } finally {
    deleting.value = ''
  }
}

function variantHint(t: TagRow): string {
  const others = (t.variants ?? []).filter(v => v !== t.tag)
  return others.length ? `Also spelled: ${others.join(', ')}` : ''
}

async function load() {
  const [tagRows, sessionRows] = await Promise.all([
    listTags({ q: q.value || undefined, origin: origin.value === 'all' ? undefined : origin.value }),
    listTagTrainingSessions(),
  ])
  tags.value = tagRows
  sessions.value = sessionRows.filter(s => s.status !== 'archived')
}

async function loadAliases() {
  const [c, m] = await Promise.all([listTagAliases('candidate'), listTagAliases('active')])
  candidates.value = c
  merged.value = m
}

async function refreshAll() {
  await Promise.all([load(), loadAliases()])
}

async function runScan() {
  scanning.value = true
  try {
    const res = await scanTagAliases()
    toast.push(`${res.proposed} new proposal${res.proposed === 1 ? '' : 's'} over ${res.tags} tags`, 'info')
    await loadAliases()
  } catch (e: unknown) {
    toast.push(errorMessage(e), 'error')
  } finally {
    scanning.value = false
  }
}

async function decide(id: number, accept: boolean) {
  try {
    if (accept) await acceptTagAlias(id)
    else await rejectTagAlias(id)
    await refreshAll()
  } catch (e: unknown) {
    toast.push(errorMessage(e), 'error')
  }
}

async function manualMerge() {
  try {
    await mergeTags(mergeAlias.value.trim(), mergeCanonical.value.trim())
    mergeAlias.value = ''
    mergeCanonical.value = ''
    await refreshAll()
  } catch (e: unknown) {
    toast.push(errorMessage(e), 'error')
  }
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
  } catch (e: unknown) {
    toast.push(errorMessage(e), 'error')
  } finally {
    trainBusy.value = false
  }
}

onMounted(refreshAll)
</script>
<style scoped>
.section-title { font-size: 15px; font-weight: 600; margin-top: 28px; margin-bottom: 6px }
.mb-2 { margin-bottom: 8px }
.mb-3 { margin-bottom: 16px }
.btn-sm { font-size: 11px; padding: 4px 8px; text-decoration: none; display: inline-block }
.btn-danger { background: #FCEBEB; color: #A32D2D; border-color: #F09595; margin-left: 4px }
.btn-danger:hover { background: #f8d9d9 }
.variant-count { font-size: 11px; margin-left: 6px }
.merge-form { display: inline-flex; align-items: center; gap: 6px; margin-left: 12px }
.merge-input { width: 160px }
.examples { font-size: 11px; margin-top: 2px; max-width: 360px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap }
.nowrap { white-space: nowrap }
.badge-labeling { background: #FAEEDA; color: #854F0B }
.badge-accepted { background: #E8F4E1; color: #2D5A1E }
</style>
