import { defineStore } from 'pinia'
import { ref, shallowRef } from 'vue'
import * as api from '@/api/client'
import { useToastStore } from './toast'

const PAGE_SIZE = 48

export const useBrowseStore = defineStore('browse', () => {
  const sources       = ref<string[]>([])
  const sourceTags    = ref<string[]>([])
  const overlayTags   = ref<string[]>([])
  const tagRows       = ref<{ source: api.TagRow[]; overlay: api.TagRow[] }>({ source: [], overlay: [] })
  const documents     = shallowRef<api.DocumentListItem[]>([])
  const total         = ref(0)
  const loading       = ref(false)
  const loadingMore   = ref(false)
  const loadingTags   = ref(false)
  const error         = ref<string | null>(null)

  function listParams(offset = 0) {
    return {
      sources: sources.value.length ? sources.value : undefined,
      source_tags: sourceTags.value.length ? sourceTags.value : undefined,
      overlay_tags: overlayTags.value.length ? overlayTags.value : undefined,
      limit: PAGE_SIZE,
      offset,
    }
  }

  async function loadTags() {
    loadingTags.value = true
    try {
      const scope = sources.value.length ? sources.value : undefined
      const [sourceRows, allRows] = await Promise.all([
        api.listTags({ origin: 'source', sources: scope, limit: 200 }),
        api.listTags({ sources: scope, limit: 200 }),
      ])
      tagRows.value = {
        source: sourceRows,
        overlay: allRows.filter(t => t.origin !== 'source'),
      }
    } catch (e: any) {
      useToastStore().push(e.message, 'error')
    } finally {
      loadingTags.value = false
    }
  }

  async function load() {
    loading.value = true
    error.value   = null
    try {
      const res = await api.listDocuments(listParams(0))
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
      const res = await api.listDocuments(listParams(documents.value.length))
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
    loadTags()
    load()
  }

  function toggleSourceTag(tag: string) {
    const idx = sourceTags.value.indexOf(tag)
    if (idx === -1) sourceTags.value.push(tag)
    else sourceTags.value.splice(idx, 1)
    load()
  }

  function toggleOverlayTag(tag: string) {
    const idx = overlayTags.value.indexOf(tag)
    if (idx === -1) overlayTags.value.push(tag)
    else overlayTags.value.splice(idx, 1)
    load()
  }

  return {
    sources,
    sourceTags,
    overlayTags,
    tagRows,
    documents,
    total,
    loading,
    loadingMore,
    loadingTags,
    error,
    load,
    loadTags,
    loadMore,
    toggleSource,
    toggleSourceTag,
    toggleOverlayTag,
  }
})
