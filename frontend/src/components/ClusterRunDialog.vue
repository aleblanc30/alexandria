<template>
  <Teleport to="body">
    <div v-if="open" class="modal-overlay" @click.self="cancel">
      <div
        ref="panelRef"
        class="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="cluster-run-dialog-title"
        tabindex="-1"
      >
        <h2 id="cluster-run-dialog-title" class="modal-title">New clustering run</h2>

        <div class="form-field">
          <label for="crd-method">Method</label>
          <select id="crd-method" ref="firstFieldRef" v-model="method">
            <option value="pca">PCA → HDBSCAN</option>
            <option value="legacy_umap">UMAP → HDBSCAN (legacy)</option>
            <option value="agglomerative">PCA → Agglomerative</option>
          </select>
        </div>

        <div class="form-field form-field--checkbox">
          <label><input type="checkbox" v-model="autoTune" /> Auto-tune parameters</label>
        </div>

        <template v-if="!autoTune && method !== 'agglomerative'">
          <div class="form-field">
            <label for="crd-mcs">Min cluster size</label>
            <input id="crd-mcs" v-model.number="minClusterSize" type="number" min="2" max="1000" />
          </div>
          <div class="form-field">
            <label for="crd-ms">Min samples</label>
            <input id="crd-ms" v-model.number="minSamples" type="number" min="1" max="1000" />
          </div>
          <div class="form-field">
            <label for="crd-nn">Neighbours</label>
            <input id="crd-nn" v-model.number="nNeighbors" type="number" min="2" max="200" />
          </div>
        </template>

        <template v-if="method === 'agglomerative'">
          <div class="form-field">
            <label for="crd-linkage">Linkage</label>
            <select id="crd-linkage" v-model="linkage">
              <option value="ward">Ward</option>
              <option value="average">Average</option>
              <option value="complete">Complete</option>
              <option value="single">Single</option>
            </select>
          </div>
          <template v-if="!autoTune">
            <div class="form-field">
              <label for="crd-kmode">Cluster count</label>
              <select id="crd-kmode" v-model="kMode">
                <option value="count">Fixed count</option>
                <option value="distance">Distance threshold</option>
              </select>
            </div>
            <div v-if="kMode === 'count'" class="form-field">
              <label for="crd-nclusters">Number of clusters</label>
              <input id="crd-nclusters" v-model.number="nClusters" type="number" min="2" max="1000" />
            </div>
            <div v-else class="form-field">
              <label for="crd-distthresh">Distance threshold</label>
              <input id="crd-distthresh" v-model.number="distanceThreshold" type="number" step="0.01" min="0.001" />
            </div>
          </template>
        </template>

        <div class="form-field">
          <label for="crd-mindist">Min distance</label>
          <input id="crd-mindist" v-model.number="minDist" type="number" step="0.01" min="0" max="1" />
        </div>

        <div v-if="method === 'legacy_umap'" class="form-field">
          <label for="crd-umapdim">UMAP dims</label>
          <input id="crd-umapdim" v-model.number="nComponents" type="number" min="2" max="50" />
        </div>
        <div v-else class="form-field">
          <label for="crd-pca">PCA components</label>
          <input id="crd-pca" v-model.number="pcaComponents" type="number" min="2" max="500" placeholder="50 (default)" />
        </div>

        <div class="form-field form-field--checkbox">
          <label><input v-model="skipLabelling" type="checkbox" /> Skip LLM labelling (TF-IDF only)</label>
        </div>
        <div class="form-field form-field--checkbox">
          <label>
            <input v-model="asyncLabelling" type="checkbox" :disabled="skipLabelling" />
            Label in background
          </label>
        </div>

        <div class="modal-actions">
          <button type="button" class="btn" :disabled="busy" @click="cancel">Cancel</button>
          <button type="button" class="btn btn--primary" :disabled="busy" @click="submit">
            {{ busy ? 'Starting…' : 'Start run' }}
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import type { ClusterRunParams } from '@/api/client'

const props = withDefaults(defineProps<{
  open: boolean
  busy?: boolean
}>(), {
  busy: false,
})

const emit = defineEmits<{
  'update:open': [value: boolean]
  submit: [params: ClusterRunParams]
}>()

const method            = ref<'pca' | 'legacy_umap' | 'agglomerative'>('pca')
const autoTune          = ref(true)
const minClusterSize    = ref<number | undefined>(undefined)
const minSamples        = ref<number | undefined>(undefined)
const nNeighbors        = ref<number | undefined>(undefined)
const minDist           = ref(0.1)
const pcaComponents     = ref<number | undefined>(undefined)
const nComponents       = ref(5)
const linkage           = ref<'ward' | 'average' | 'complete' | 'single'>('ward')
const kMode             = ref<'count' | 'distance'>('count')
const nClusters         = ref<number | undefined>(undefined)
const distanceThreshold = ref<number | undefined>(undefined)
const skipLabelling     = ref(false)
const asyncLabelling    = ref(false)

const panelRef      = ref<HTMLElement | null>(null)
const firstFieldRef = ref<HTMLSelectElement | null>(null)

watch(() => props.open, async (isOpen) => {
  if (!isOpen) return
  method.value = 'pca'
  autoTune.value = true
  minClusterSize.value = undefined
  minSamples.value = undefined
  nNeighbors.value = undefined
  minDist.value = 0.1
  pcaComponents.value = undefined
  nComponents.value = 5
  linkage.value = 'ward'
  kMode.value = 'count'
  nClusters.value = undefined
  distanceThreshold.value = undefined
  skipLabelling.value = false
  asyncLabelling.value = false
  await nextTick()
  firstFieldRef.value?.focus()
})

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape' && props.open) cancel()
}

watch(() => props.open, (isOpen) => {
  if (isOpen) window.addEventListener('keydown', onKeydown)
  else window.removeEventListener('keydown', onKeydown)
})

function cancel() {
  if (props.busy) return
  emit('update:open', false)
}

function submit() {
  if (props.busy) return
  const params: ClusterRunParams = {
    cluster_space: method.value,
    min_dist: minDist.value,
    skip_labelling: skipLabelling.value,
    async_labelling: asyncLabelling.value,
  }
  if (method.value === 'agglomerative') {
    params.linkage = linkage.value
    if (!autoTune.value) {
      if (kMode.value === 'count') {
        params.n_clusters = nClusters.value ?? null
      } else {
        params.distance_threshold = distanceThreshold.value ?? null
      }
    }
    if (pcaComponents.value != null) params.pca_components = pcaComponents.value
  } else {
    params.min_cluster_size = autoTune.value ? null : (minClusterSize.value ?? null)
    params.min_samples = autoTune.value ? null : (minSamples.value ?? null)
    params.n_neighbors = autoTune.value ? null : (nNeighbors.value ?? null)
    if (method.value === 'pca') {
      if (pcaComponents.value != null) params.pca_components = pcaComponents.value
    } else {
      params.n_components = nComponents.value
    }
  }
  emit('submit', params)
}
</script>
