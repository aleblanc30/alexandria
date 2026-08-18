import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import type { DocumentDetail } from '@/api/client'
import { visibleSources } from '@/constants/sources'
import { useBrowseStore } from './browse'

const EXPERIMENTAL_SOURCES_KEY = 'alexandria-show-experimental-sources'

function loadShowExperimentalSources(): boolean {
  try {
    return localStorage.getItem(EXPERIMENTAL_SOURCES_KEY) === '1'
  } catch { /* ignore */ }
  return false
}

export const useUiStore = defineStore('ui', () => {
  const activeDocId   = ref<number | null>(null)
  const activeDoc     = ref<DocumentDetail | null>(null)
  const detailOpen    = ref(false)

  const showExperimentalSources = ref(loadShowExperimentalSources())
  const sources = computed(() => visibleSources(showExperimentalSources.value))

  function setShowExperimentalSources(show: boolean) {
    showExperimentalSources.value = show
    try {
      localStorage.setItem(EXPERIMENTAL_SOURCES_KEY, show ? '1' : '0')
    } catch { /* ignore */ }
    if (!show) useBrowseStore().pruneSources(sources.value)
  }

  function openDetail(doc: DocumentDetail) {
    activeDoc.value  = doc
    activeDocId.value = doc.id
    detailOpen.value = true
  }
  function closeDetail() {
    detailOpen.value  = false
    activeDocId.value = null
  }

  return {
    activeDocId,
    activeDoc,
    detailOpen,
    openDetail,
    closeDetail,
    showExperimentalSources,
    sources,
    setShowExperimentalSources,
  }
})
