<template>
  <div>
    <div class="page-header">
      <div>
        <h1 class="page-title">Cluster explorer</h1>
        <p class="page-sub">{{ store.list.length }} clusters · UMAP 2D projection</p>
      </div>
      <button
        v-if="store.list.length"
        class="btn btn--primary"
        :disabled="applyingAll"
        @click="applyAll"
      >
        {{ applyingAll ? 'Applying…' : 'Apply all tag suggestions' }}
      </button>
    </div>

    <div class="card mb-4">
      <div class="card-label">UMAP projection — coloured by cluster</div>
      <div class="scatter-wrap">
        <ScatterPlot :points="store.scatter" :clusters="store.list" />
      </div>
    </div>

    <div class="cluster-grid">
      <div v-for="c in store.list" :key="c.cluster_id" class="cluster-card">
        <div class="cluster-name">{{ c.label }}</div>
        <div class="cluster-desc">{{ c.description }}</div>
        <div class="cluster-count">{{ c.doc_count }} documents</div>
        <div class="cluster-bar-bg">
          <div class="cluster-bar-fill"
               :style="{ width: (c.doc_count / maxCount * 100) + '%', background: colorFor(c.cluster_id) }" />
        </div>

        <div class="cluster-tag-section" @click.stop>
          <div class="cluster-tag-label">Tag suggestions</div>
          <div v-if="c.tag_candidates?.length" class="cluster-candidates">
            <button
              v-for="candidate in c.tag_candidates"
              :key="candidate.tag + candidate.source"
              type="button"
              class="candidate-chip"
              :class="{ 'candidate-chip--active': tagEdits[c.cluster_id] === candidate.tag }"
              @click="selectCandidate(c, candidate.tag)"
            >
              <span>#{{ candidate.tag }}</span>
              <span class="candidate-meta">{{ candidateLabel(candidate) }}</span>
            </button>
          </div>
          <p v-if="c.llm_error" class="cluster-llm-error">{{ c.llm_error }}</p>
          <p v-else-if="!c.tag_candidates?.length" class="hint-text">No tag suggestions yet</p>

          <div class="cluster-tag-row">
            <input
              v-model="tagEdits[c.cluster_id]"
              class="cluster-tag-input"
              :placeholder="c.suggested_tag"
            />
            <button
              class="btn-xs"
              :disabled="regeneratingId === c.cluster_id"
              @click="regenerateOne(c)"
            >
              {{ regeneratingId === c.cluster_id ? '…' : 'Regenerate' }}
            </button>
            <button
              class="btn-xs btn-xs--ok"
              :disabled="applyingId === c.cluster_id"
              @click="applyOne(c)"
            >
              {{ applyingId === c.cluster_id ? '…' : 'Apply' }}
            </button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
<script setup lang="ts">
import { ref, computed, onMounted, reactive } from 'vue'
import { useClustersStore } from '@/stores/clusters'
import type { ClusterOut, TagCandidate } from '@/api/client'
import ScatterPlot from '@/components/ScatterPlot.vue'

const store           = useClustersStore()
const tagEdits        = reactive<Record<number, string>>({})
const applyingId      = ref<number | null>(null)
const applyingAll     = ref(false)
const regeneratingId  = ref<number | null>(null)
const PALETTE         = ['#378ADD','#7F77DD','#639922','#BA7517','#1D9E75','#D85A30','#D4537E','#888780']
const colorFor        = (id: number) => PALETTE[id % PALETTE.length]
const maxCount        = computed(() => Math.max(1, ...store.list.map(c => c.doc_count)))

function syncEdits(clusters: ClusterOut[]) {
  for (const c of clusters) {
    tagEdits[c.cluster_id] = c.suggested_tag
  }
}

function candidateLabel(candidate: TagCandidate) {
  if (candidate.source === 'existing' && candidate.coverage > 0) {
    return `${Math.round(candidate.coverage * 100)}%`
  }
  if (candidate.source === 'llm') return 'AI'
  return candidate.source
}

function selectCandidate(c: ClusterOut, tag: string) {
  tagEdits[c.cluster_id] = tag
}

function tagFor(c: ClusterOut) {
  return (tagEdits[c.cluster_id] || c.suggested_tag).trim()
}

async function applyOne(c: ClusterOut) {
  applyingId.value = c.cluster_id
  try {
    await store.applyTag(c.cluster_id, tagFor(c))
  } finally {
    applyingId.value = null
  }
}

async function regenerateOne(c: ClusterOut) {
  regeneratingId.value = c.cluster_id
  try {
    const updated = await store.regenerateTag(c.cluster_id)
    tagEdits[c.cluster_id] = updated.suggested_tag
  } finally {
    regeneratingId.value = null
  }
}

async function applyAll() {
  applyingAll.value = true
  try {
    await store.applyAllTags()
  } finally {
    applyingAll.value = false
  }
}

onMounted(async () => {
  await store.loadClusters()
  await store.loadScatter()
  syncEdits(store.list)
})
</script>
