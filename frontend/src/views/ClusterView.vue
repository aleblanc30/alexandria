<template>
  <div>
    <div class="page-header">
      <div>
        <h1 class="page-title">Cluster explorer</h1>
        <p class="page-sub">{{ l1Count }} level-1 · {{ l2Count }} level-2 clusters · UMAP 2D projection</p>
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
      <div class="card-label">UMAP projection — coloured by level-1 cluster</div>
      <div class="scatter-wrap">
        <ScatterPlot :points="store.scatter" :clusters="l1Clusters" />
      </div>
    </div>

    <div class="cluster-groups">
      <section v-for="group in groupedClusters" :key="group.l1.cluster_id" class="cluster-group">
        <div
          v-for="c in [group.l1, ...group.children]"
          :key="c.cluster_id"
          class="cluster-card"
          :class="c.level === 2 ? 'cluster-card--l2' : 'cluster-card--l1'"
        >
          <div
            class="cluster-level-badge"
            :class="{ 'cluster-level-badge--l2': c.level === 2 }"
          >
            {{ c.level === 2 ? 'Level 2' : 'Level 1' }}
          </div>
          <div v-if="c.level === 2 && c.parent_label" class="cluster-parent">{{ c.parent_label }}</div>
          <div class="cluster-name">{{ c.label }}</div>
          <div class="cluster-desc">{{ c.description }}</div>
          <div class="cluster-count">{{ c.doc_count }} documents</div>
          <div class="cluster-bar-bg">
            <div class="cluster-bar-fill"
                 :style="{ width: (c.doc_count / maxCount * 100) + '%', background: colorFor(group.l1.cluster_id) }" />
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
      </section>
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

const l1Clusters = computed(() => store.list.filter(c => (c.level ?? 1) === 1))
const l2Count = computed(() => store.list.filter(c => c.level === 2).length)
const l1Count = computed(() => l1Clusters.value.length)

const groupedClusters = computed(() => {
  const l1 = store.list.filter(c => (c.level ?? 1) === 1)
  const l2ByParent = new Map<number, ClusterOut[]>()
  for (const c of store.list) {
    if (c.level === 2 && c.parent_cluster_id != null) {
      const arr = l2ByParent.get(c.parent_cluster_id) ?? []
      arr.push(c)
      l2ByParent.set(c.parent_cluster_id, arr)
    }
  }
  return l1.map(l1c => ({
    l1: l1c,
    children: l2ByParent.get(l1c.cluster_id) ?? [],
  }))
})

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
<style scoped>
.cluster-groups {
  display: flex;
  flex-direction: column;
  gap: 20px;
}
.cluster-group {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.cluster-card--l2 {
  margin-left: 20px;
  border-left: 3px solid #E8F1FA;
}
.cluster-level-badge {
  font-size: 9px;
  text-transform: uppercase;
  letter-spacing: .08em;
  color: #1E4D72;
  margin-bottom: 6px;
}
.cluster-level-badge--l2 {
  color: #5A2D82;
}
.cluster-parent {
  font-size: 11px;
  color: var(--hint);
  margin-bottom: 4px;
}
</style>
