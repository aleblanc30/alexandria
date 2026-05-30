import { defineStore } from 'pinia'
import { ref } from 'vue'
import type { DocumentDetail } from '@/api/client'

export const useUiStore = defineStore('ui', () => {
  const activeDocId   = ref<number | null>(null)
  const activeDoc     = ref<DocumentDetail | null>(null)
  const detailOpen    = ref(false)

  function openDetail(doc: DocumentDetail) {
    activeDoc.value  = doc
    activeDocId.value = doc.id
    detailOpen.value = true
  }
  function closeDetail() {
    detailOpen.value  = false
    activeDocId.value = null
  }

  return { activeDocId, activeDoc, detailOpen, openDetail, closeDetail }
})
