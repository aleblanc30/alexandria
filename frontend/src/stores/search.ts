import { defineStore } from 'pinia'
import { ref, shallowRef } from 'vue'
import * as api from '@/api/client'
import { useToastStore } from './toast'
import type { useBrowseStore } from './browse'
import { resolveGeneralTags } from './browse'

export const SEARCH_PAGE_SIZE = 48

export const useSearchStore = defineStore('search', () => {
  const query       = ref('')
  const mode        = ref<'semantic'|'fulltext'|'hybrid'>('semantic')
  const results     = shallowRef<api.DocumentOut[]>([])
  const images      = shallowRef<api.ImageOut[]>([])
  const total       = ref(0)
  const loading     = ref(false)
  const loadingMore = ref(false)
  const error       = ref<string | null>(null)
  const includeImgs = ref(false)

  function browseSearchBody(browse: ReturnType<typeof useBrowseStore>, offset = 0): api.SearchRequest {
    const general_tags = resolveGeneralTags(browse.academicFilter, browse.academicKinds)
    return {
      query: query.value,
      mode: mode.value,
      sources: browse.sources.length ? browse.sources : undefined,
      source_tags: browse.sourceTags.length ? browse.sourceTags : undefined,
      general_tags,
      cluster_l1_tags: browse.level1Tags.length ? browse.level1Tags : undefined,
      cluster_l2_tags: browse.level2Tags.length ? browse.level2Tags : undefined,
      wayback_only: browse.waybackOnly || undefined,
      limit: SEARCH_PAGE_SIZE,
      offset,
      include_images: includeImgs.value,
    }
  }

  async function runWithBrowseFilters(
    browse: ReturnType<typeof useBrowseStore>,
    append = false,
  ) {
    if (!query.value.trim()) return
    if (append) {
      if (loadingMore.value || loading.value) return
      if (results.value.length >= total.value) return
      loadingMore.value = true
    } else {
      loading.value = true
      error.value = null
    }
    try {
      const res = await api.search(
        browseSearchBody(browse, append ? results.value.length : 0),
      )
      if (append) {
        results.value = [...results.value, ...res.documents]
      } else {
        results.value = res.documents
      }
      images.value = res.images
      total.value = res.total
    } catch (e: any) {
      error.value = e.message
      useToastStore().push(e.message, 'error')
    } finally {
      loading.value = false
      loadingMore.value = false
    }
  }

  function reset() {
    results.value = []
    images.value = []
    total.value = 0
    error.value = null
  }

  return {
    query,
    mode,
    results,
    images,
    total,
    loading,
    loadingMore,
    error,
    includeImgs,
    runWithBrowseFilters,
    reset,
  }
})
