import { defineStore } from 'pinia'
import { ref, shallowRef } from 'vue'
import * as api from '@/api/client'
import { useToastStore } from './toast'

const PAGE_SIZE = 48

export const useBrowseStore = defineStore('browse', () => {
  const sources       = ref<string[]>([])
  const waybackOnly   = ref(false)
  const sourceTags    = ref<string[]>([])
  const level1Tags    = ref<string[]>([])
  const level2Tags    = ref<string[]>([])
  const tagRows       = ref<{ source: api.TagRow[]; level1: api.TagRow[]; level2: api.TagRow[] }>({
    source: [],
    level1: [],
    level2: [],
  })
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
      cluster_l1_tags: level1Tags.value.length ? level1Tags.value : undefined,
      cluster_l2_tags: level2Tags.value.length ? level2Tags.value : undefined,
      wayback_only: waybackOnly.value || undefined,
      limit: PAGE_SIZE,
      offset,
    }
  }

  function tagScopeParams() {
    return {
      source_tags: sourceTags.value.length ? sourceTags.value : undefined,
      cluster_l1_tags: level1Tags.value.length ? level1Tags.value : undefined,
      cluster_l2_tags: level2Tags.value.length ? level2Tags.value : undefined,
      wayback_only: waybackOnly.value || undefined,
    }
  }

  async function loadTags() {
    loadingTags.value = true
    try {
      const scope = sources.value.length ? sources.value : undefined
      const tagScope = tagScopeParams()
      const [sourceRows, level1Rows, level2Rows] = await Promise.all([
        api.listTags({ origin: 'source', sources: scope, ...tagScope, limit: 200 }),
        api.listTags({ origin: 'cluster_l1', sources: scope, ...tagScope, limit: 200 }),
        api.listTags({ origin: 'cluster_l2', sources: scope, ...tagScope, limit: 200 }),
      ])
      tagRows.value = { source: sourceRows, level1: level1Rows, level2: level2Rows }
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
    if (waybackOnly.value && s === 'firefox' && idx !== -1) {
      waybackOnly.value = false
    }
    loadTags()
    load()
  }

  function toggleWayback() {
    waybackOnly.value = !waybackOnly.value
    if (waybackOnly.value) {
      sources.value = ['firefox']
    } else {
      sources.value = []
    }
    loadTags()
    void load()
  }

  function toggleSourceTag(tag: string) {
    const idx = sourceTags.value.indexOf(tag)
    if (idx === -1) sourceTags.value.push(tag)
    else sourceTags.value.splice(idx, 1)
    loadTags()
    load()
  }

  function toggleLevel1Tag(tag: string) {
    const idx = level1Tags.value.indexOf(tag)
    if (idx === -1) level1Tags.value.push(tag)
    else level1Tags.value.splice(idx, 1)
    loadTags()
    load()
  }

  function toggleLevel2Tag(tag: string) {
    const idx = level2Tags.value.indexOf(tag)
    if (idx === -1) level2Tags.value.push(tag)
    else level2Tags.value.splice(idx, 1)
    loadTags()
    load()
  }

  return {
    sources,
    waybackOnly,
    sourceTags,
    level1Tags,
    level2Tags,
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
    toggleWayback,
    toggleSourceTag,
    toggleLevel1Tag,
    toggleLevel2Tag,
  }
})
