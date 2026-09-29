<template>
  <section class="dup-panel">
    <div class="dup-head">
      <h2 class="dup-title">Duplicate documents</h2>
      <button type="button" class="btn btn-sm" :disabled="scanning" @click="runScan">
        {{ scanning ? 'Scanning…' : 'Find duplicates' }}
      </button>
    </div>
    <p class="dup-sub">
      Documents sharing a DOI, arXiv id, ISBN or URL are linked on scan and shown as one item.
      Near duplicates found by document similarity wait for your review. Nothing is deleted;
      unlinking restores both.
    </p>

    <div v-if="!candidates.length" class="dup-hint">No near duplicates waiting.</div>
    <table v-else class="dup-table">
      <thead>
        <tr><th>Document</th><th>Same as</th><th>Similarity</th><th /></tr>
      </thead>
      <tbody>
        <tr v-for="c in candidates" :key="c.id">
          <td><DupDoc :doc="c.duplicate" /></td>
          <td><DupDoc :doc="c.canonical" /></td>
          <td class="dup-hint">{{ c.score != null ? c.score.toFixed(3) : c.match_key }}</td>
          <td class="dup-actions">
            <button type="button" class="btn btn-sm" @click="decide(c.id, true)">Link</button>
            <button type="button" class="btn btn-sm" @click="decide(c.id, false)">Keep apart</button>
          </td>
        </tr>
      </tbody>
    </table>

    <details v-if="linked.length" class="dup-linked">
      <summary class="dup-hint">{{ linked.length }} linked pair{{ linked.length > 1 ? 's' : '' }}</summary>
      <table class="dup-table">
        <tbody>
          <tr v-for="l in linked" :key="l.id">
            <td><DupDoc :doc="l.duplicate" /></td>
            <td><DupDoc :doc="l.canonical" /></td>
            <td class="dup-hint" :title="l.match_value ?? ''">{{ l.match_key }}</td>
            <td class="dup-actions">
              <button type="button" class="btn btn-sm" @click="decide(l.id, false)">Unlink</button>
            </td>
          </tr>
        </tbody>
      </table>
    </details>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import {
  acceptDuplicate,
  listDuplicates,
  rejectDuplicate,
  scanDuplicates,
  type DuplicateLink,
} from '@/api/client'
import { errorMessage } from '@/lib/notifyError'
import { useToastStore } from '@/stores/toast'
import DupDoc from './DupDoc.vue'

const toast = useToastStore()
const candidates = ref<DuplicateLink[]>([])
const linked = ref<DuplicateLink[]>([])
const scanning = ref(false)

async function load() {
  const [c, l] = await Promise.all([listDuplicates('candidate'), listDuplicates('merged')])
  candidates.value = c
  linked.value = l
}

async function runScan() {
  scanning.value = true
  try {
    const r = await scanDuplicates()
    toast.push(`${r.linked} linked, ${r.proposed} near duplicate${r.proposed === 1 ? '' : 's'} to review`)
    await load()
  } catch (e: unknown) {
    toast.push(errorMessage(e), 'error')
  } finally {
    scanning.value = false
  }
}

async function decide(id: number, link: boolean) {
  try {
    if (link) await acceptDuplicate(id)
    else await rejectDuplicate(id)
    await load()
  } catch (e: unknown) {
    toast.push(errorMessage(e), 'error')
  }
}

onMounted(() => {
  load().catch((e: unknown) => toast.push(errorMessage(e), 'error'))
})
</script>

<style scoped>
.dup-panel { margin-top: 24px }
.dup-head { display: flex; align-items: center; gap: 12px }
.dup-title { font-size: 15px; font-weight: 600; margin: 0 }
.dup-sub { font-size: 12px; color: var(--muted); margin: 6px 0 10px }
.dup-hint { font-size: 12px; color: var(--hint) }
.dup-table { width: 100%; border-collapse: collapse; font-size: 12px; margin-top: 6px }
.dup-table th { text-align: left; font-weight: 500; color: var(--hint); padding: 4px 6px }
.dup-table td { padding: 6px; border-top: 0.5px solid var(--border); vertical-align: top }
.dup-actions { white-space: nowrap; text-align: right }
.dup-linked { margin-top: 12px }
.btn-sm { font-size: 11px; padding: 4px 8px }
</style>
