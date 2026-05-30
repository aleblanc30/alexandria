import { defineStore } from 'pinia'
import { ref, shallowRef } from 'vue'
import * as api from '@/api/client'
import { useToastStore } from './toast'

export const useSearchStore = defineStore('search', () => {
  const query       = ref('')
  const mode        = ref<'semantic'|'fulltext'|'hybrid'>('semantic')
  const sources     = ref<string[]>([])
  const results     = shallowRef<api.DocumentOut[]>([])
  const images      = shallowRef<api.ImageOut[]>([])
  const total       = ref(0)
  const loading     = ref(false)
  const error       = ref<string | null>(null)
  const limit       = ref(20)
  const offset      = ref(0)
  const includeImgs = ref(false)

  async function run() {
    if (!query.value.trim()) return
    loading.value = true
    error.value   = null
    try {
      const res = await api.search({
        query: query.value, mode: mode.value,
        sources: sources.value, limit: limit.value,
        offset: offset.value, include_images: includeImgs.value,
      })
      results.value = res.documents
      images.value  = res.images
      total.value   = res.total
    } catch (e: any) {
      error.value = e.message
      useToastStore().push(e.message, 'error')
    } finally {
      loading.value = false
    }
  }

  function reset() {
    results.value = []; images.value = []; total.value = 0; offset.value = 0
  }

  return { query, mode, sources, results, images, total, loading, error,
           limit, offset, includeImgs, run, reset }
})
