import { defineStore } from 'pinia'
import { ref, shallowRef } from 'vue'
import * as api from '@/api/client'
import { PAGE_SIZE } from '@/constants/pagination'
import { buildDocumentFilters, type AcademicKind } from '@/lib/browseFilters'
import { useSearchStore } from './search'
import { useToastStore } from './toast'

const VIEW_MODE_KEY = 'alexandria-browse-view-mode'

export type BrowseViewMode = 'cards' | 'lines'

function loadViewMode(): BrowseViewMode {
  try {
    const v = localStorage.getItem(VIEW_MODE_KEY)
    if (v === 'lines' || v === 'cards') return v
  } catch { /* ignore */ }
  return 'cards'
}

export type { AcademicKind } from '@/lib/browseFilters'
export { resolveGeneralTags } from '@/lib/browseFilters'

export const useBrowseStore = defineStore('browse', () => {
  const sources       = ref<string[]>([])
  const waybackOnly   = ref(false)
  const academicFilter = ref(false)
  const academicKinds  = ref<AcademicKind[]>([])
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
  const viewMode      = ref<BrowseViewMode>(loadViewMode())
  const selectedDocIds  = ref<Set<number>>(new Set())

  function isDocSelected(id: number) {
    return selectedDocIds.value.has(id)
  }

  function toggleDocSelection(id: number) {
    const next = new Set(selectedDocIds.value)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    selectedDocIds.value = next
  }

  function clearSelection() {
    selectedDocIds.value = new Set()
  }

  function selectAllOnPage(ids: number[]) {
    selectedDocIds.value = new Set(ids)
  }

  function setViewMode(mode: BrowseViewMode) {
    viewMode.value = mode
    try {
      localStorage.setItem(VIEW_MODE_KEY, mode)
    } catch { /* ignore */ }
  }

  function filterState() {
    return {
      sources: sources.value,
      sourceTags: sourceTags.value,
      level1Tags: level1Tags.value,
      level2Tags: level2Tags.value,
      academicFilter: academicFilter.value,
      academicKinds: academicKinds.value,
      waybackOnly: waybackOnly.value,
    }
  }

  function listParams(offset = 0) {
    return {
      ...buildDocumentFilters(filterState(), { includeSources: true }),
      limit: PAGE_SIZE,
      offset,
    }
  }

  function tagScopeParams() {
    return buildDocumentFilters(filterState())
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

  async function refresh() {
    const search = useSearchStore()
    if (search.query.trim()) {
      await search.runWithBrowseFilters(useBrowseStore())
      return
    }
    await loadTags()
    await load()
  }

  async function refreshTagsAndList() {
    await loadTags()
    await refresh()
  }

  function toggleSource(s: string) {
    const idx = sources.value.indexOf(s)
    if (idx === -1) sources.value.push(s)
    else sources.value.splice(idx, 1)
    if (waybackOnly.value && s === 'firefox' && idx !== -1) {
      waybackOnly.value = false
    }
    void refreshTagsAndList()
  }

  function toggleWayback() {
    waybackOnly.value = !waybackOnly.value
    if (waybackOnly.value) {
      sources.value = ['firefox']
    } else {
      sources.value = []
    }
    void refreshTagsAndList()
  }

  function toggleAcademic() {
    academicFilter.value = !academicFilter.value
    if (!academicFilter.value) {
      academicKinds.value = []
    }
    void refreshTagsAndList()
  }

  function toggleAcademicKind(kind: AcademicKind) {
    const idx = academicKinds.value.indexOf(kind)
    if (idx === -1) academicKinds.value.push(kind)
    else academicKinds.value.splice(idx, 1)
    void refreshTagsAndList()
  }

  function toggleSourceTag(tag: string) {
    const idx = sourceTags.value.indexOf(tag)
    if (idx === -1) sourceTags.value.push(tag)
    else sourceTags.value.splice(idx, 1)
    void refreshTagsAndList()
  }

  function toggleLevel1Tag(tag: string) {
    const idx = level1Tags.value.indexOf(tag)
    if (idx === -1) level1Tags.value.push(tag)
    else level1Tags.value.splice(idx, 1)
    void refreshTagsAndList()
  }

  function toggleLevel2Tag(tag: string) {
    const idx = level2Tags.value.indexOf(tag)
    if (idx === -1) level2Tags.value.push(tag)
    else level2Tags.value.splice(idx, 1)
    void refreshTagsAndList()
  }

  return {
    sources,
    waybackOnly,
    academicFilter,
    academicKinds,
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
    viewMode,
    setViewMode,
    selectedDocIds,
    isDocSelected,
    toggleDocSelection,
    clearSelection,
    selectAllOnPage,
    load,
    loadTags,
    loadMore,
    refresh,
    toggleSource,
    toggleWayback,
    toggleAcademic,
    toggleAcademicKind,
    toggleSourceTag,
    toggleLevel1Tag,
    toggleLevel2Tag,
  }
})
