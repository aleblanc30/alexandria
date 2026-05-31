import { defineStore } from 'pinia'
import { ref, shallowRef } from 'vue'
import * as api from '@/api/client'
import { useToastStore } from './toast'

const PAGE_SIZE = 48

export const useBrowseStore = defineStore('browse', () => {
  const sources     = ref<string[]>([])
  const documents   = shallowRef<api.DocumentListItem[]>([])
  const total       = ref(0)
  const loading     = ref(false)
  const loadingMore = ref(false)
  const error       = ref<string | null>(null)

  async function load() {
    loading.value = true
    error.value   = null
    try {
      const res = await api.listDocuments({
        sources: sources.value.length ? sources.value : undefined,
        limit: PAGE_SIZE,
        offset: 0,
      })
      documents.value = res.documents
      total.value     = res.total
    } catch (e: any) {
      error.value = e.message
      useToastStore().push(e.message, 'error')
    } finally {
      loading.value = false
    }
  }

  async function loadMore() {
    if (loadingMore.value || loading.value) return
    if (documents.value.length >= total.value) return
    loadingMore.value = true
    try {
      const res = await api.listDocuments({
        sources: sources.value.length ? sources.value : undefined,
        limit: PAGE_SIZE,
        offset: documents.value.length,
      })
      documents.value = [...documents.value, ...res.documents]
      total.value     = res.total
    } catch (e: any) {
      error.value = e.message
      useToastStore().push(e.message, 'error')
    } finally {
      loadingMore.value = false
    }
  }

  function toggleSource(s: string) {
    const idx = sources.value.indexOf(s)
    if (idx === -1) sources.value.push(s)
    else sources.value.splice(idx, 1)
    load()
  }

  return { sources, documents, total, loading, loadingMore, error, load, loadMore, toggleSource }
})
