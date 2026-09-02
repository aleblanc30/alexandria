<template>
  <div>
    <div class="page-header">
      <div>
        <h1 class="page-title">Cluster explorer</h1>
        <p class="page-sub">
          {{ l1Count }} level-1 · {{ l2Count }} level-2 clusters
          <template v-if="noiseCluster"> · {{ noiseCluster.doc_count }} unclustered</template>
          · UMAP 2D projection
        </p>
      </div>
      <button
        v-if="store.list.length"
        class="btn btn--primary"
        :disabled="applyingAll"
        @click="applyAll"
      >
        {{ applyingAll ? 'Applying…' : 'Apply all labels as tags' }}
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
            :class="{
              'cluster-level-badge--l2': c.level === 2,
              'cluster-level-badge--noise': c.is_noise,
            }"
          >
            {{ c.is_noise ? 'Noise' : c.level === 2 ? 'Level 2' : 'Level 1' }}
          </div>
          <div v-if="c.level === 2 && c.parent_label" class="cluster-parent">{{ c.parent_label }}</div>

          <!-- The noise bucket is a holding pen, not a topic: no label to edit,
               regenerate, or apply as a tag. -->
          <div v-if="c.is_noise" class="cluster-noise-label">{{ c.label }}</div>
          <div v-else class="cluster-label-row">
            <input
              v-model="labelEdits[c.cluster_id]"
              class="cluster-label-input"
              :class="{ 'cluster-label-input--dirty': isDirty(c.cluster_id) }"
              type="text"
              @blur="saveLabel(c)"
              @keydown.enter="($event.target as HTMLInputElement)?.blur()"
            />
            <button
              type="button"
              class="btn-xs"
              :disabled="regeneratingId === c.cluster_id"
              @click.stop="regenerateOne(c)"
            >
              {{ regeneratingId === c.cluster_id ? '…' : 'Regenerate label' }}
            </button>
            <button
              type="button"
              class="btn-xs btn-xs--ok"
              :disabled="applyingId === c.cluster_id"
              @click.stop="applyOne(c)"
            >
              {{ applyingId === c.cluster_id ? '…' : 'Apply as tag' }}
            </button>
          </div>

          <div v-if="c.description" class="cluster-desc">{{ c.description }}</div>
          <div class="cluster-count">{{ c.doc_count }} documents</div>
          <div class="cluster-bar-bg">
            <div class="cluster-bar-fill"
                 :style="{ width: (c.doc_count / maxCount * 100) + '%', background: colorFor(group.l1.cluster_id) }" />
          </div>
        </div>
      </section>
    </div>
  </div>
</template>
<script setup lang="ts">
import { ref, computed, onMounted, reactive } from 'vue'
import { useClustersStore } from '@/stores/clusters'
import type { ClusterOut } from '@/api/client'
import { colorForIndex } from '@/constants/colors'
import { slugifyTag } from '@/lib/slugifyTag'
import ScatterPlot from '@/components/ScatterPlot.vue'

const store           = useClustersStore()
const labelEdits      = reactive<Record<number, string>>({})
const savedLabels     = reactive<Record<number, string>>({})
const applyingId      = ref<number | null>(null)
const applyingAll     = ref(false)
const regeneratingId  = ref<number | null>(null)
const colorFor        = colorForIndex
// Noise is excluded from the bar scale: it is routinely the biggest bucket in a
// run, and scaling to it flattens every real cluster's bar to nothing.
const maxCount        = computed(
  () => Math.max(1, ...store.list.filter(c => !c.is_noise).map(c => c.doc_count)),
)

const l1Clusters = computed(() => store.list.filter(c => (c.level ?? 1) === 1 && !c.is_noise))
const l2Count = computed(() => store.list.filter(c => c.level === 2).length)
const l1Count = computed(() => l1Clusters.value.length)
const noiseCluster = computed(() => store.list.find(c => c.is_noise) ?? null)

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

function syncLabels(clusters: ClusterOut[]) {
  for (const c of clusters) {
    labelEdits[c.cluster_id] = c.label
    savedLabels[c.cluster_id] = c.label
  }
}

function isDirty(clusterId: number): boolean {
  return (labelEdits[clusterId] ?? '') !== (savedLabels[clusterId] ?? '')
}

async function saveLabel(c: ClusterOut) {
  const next = (labelEdits[c.cluster_id] ?? '').trim()
  if (!next || next === savedLabels[c.cluster_id]) return
  try {
    const updated = await store.patchClusterLabel(c.cluster_id, next)
    labelEdits[c.cluster_id] = updated.label
    savedLabels[c.cluster_id] = updated.label
  } catch {
    labelEdits[c.cluster_id] = savedLabels[c.cluster_id]
  }
}

async function applyOne(c: ClusterOut) {
  applyingId.value = c.cluster_id
  try {
    const tag = slugifyTag(labelEdits[c.cluster_id] || c.label)
    await store.applyTag(c.cluster_id, tag)
  } finally {
    applyingId.value = null
  }
}

async function regenerateOne(c: ClusterOut) {
  regeneratingId.value = c.cluster_id
  try {
    const updated = await store.regenerateLabel(c.cluster_id)
    labelEdits[c.cluster_id] = updated.label
    savedLabels[c.cluster_id] = updated.label
  } finally {
    regeneratingId.value = null
  }
}

async function applyAll() {
  const dirty = store.list.some(c => isDirty(c.cluster_id))
  if (dirty) {
    const ok = window.confirm(
      'Some cluster labels have unsaved edits. Apply all uses saved labels in the database only. Continue?',
    )
    if (!ok) return
  }
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
  syncLabels(store.list)
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
.cluster-level-badge--noise {
  color: var(--hint);
}
.cluster-noise-label {
  font-size: 13px;
  font-weight: 600;
  color: var(--hint);
  margin-bottom: 4px;
}
.cluster-parent {
  font-size: 11px;
  color: var(--hint);
  margin-bottom: 4px;
}
.cluster-label-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}
.cluster-label-input {
  flex: 1;
  min-width: 160px;
  font-size: 15px;
  font-weight: 600;
  color: var(--text);
  border: 1px solid transparent;
  border-radius: 4px;
  padding: 4px 8px;
  background: transparent;
}
.cluster-label-input:hover,
.cluster-label-input:focus {
  border-color: var(--border);
  background: var(--surface);
  outline: none;
}
.cluster-label-input--dirty {
  border-color: #378ADD;
}
</style>
