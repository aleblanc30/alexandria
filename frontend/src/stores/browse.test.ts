import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useBrowseStore } from './browse'

// First store test in the suite (vitest had covered lib/ and client.ts
// only). Every reducer here calls back into the API to refresh the
// list, so the client module is stubbed at the boundary — the pattern any
// further store test should follow.
vi.mock('@/api/client', () => ({
  listDocuments: vi.fn(async () => ({ total: 0, documents: [] })),
  listTags: vi.fn(async () => []),
}))

function store() {
  setActivePinia(createPinia())
  localStorage.clear()
  return useBrowseStore()
}

describe('useBrowseStore source filters', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('adds a source on the first toggle and removes it on the second', () => {
    const s = store()

    s.toggleSource('zotero')
    expect(s.sources).toEqual(['zotero'])
    s.toggleSource('zotero')
    expect(s.sources).toEqual([])
  })

  it('accumulates several sources', () => {
    const s = store()

    s.toggleSource('zotero')
    s.toggleSource('calibre')
    expect(s.sources).toEqual(['zotero', 'calibre'])
  })

  it('narrows to firefox when the wayback filter goes on', () => {
    const s = store()
    s.toggleSource('zotero')

    s.toggleWayback()

    expect(s.waybackOnly).toBe(true)
    expect(s.sources).toEqual(['firefox'])
  })

  it('drops the wayback filter when firefox is toggled back off', () => {
    const s = store()
    s.toggleWayback()

    s.toggleSource('firefox')

    expect(s.waybackOnly).toBe(false)
    expect(s.sources).toEqual([])
  })

  it('prunes sources that are no longer on offer', () => {
    const s = store()
    s.toggleSource('zotero')
    s.toggleSource('calibre')

    s.pruneSources(['zotero', 'reddit'])

    expect(s.sources).toEqual(['zotero'])
  })

  it('drops the wayback filter when pruning removes firefox', () => {
    const s = store()
    s.toggleWayback()

    s.pruneSources(['zotero'])

    expect(s.sources).toEqual([])
    expect(s.waybackOnly).toBe(false)
  })

  it('leaves everything alone when pruning removes nothing', () => {
    const s = store()
    s.toggleSource('zotero')

    s.pruneSources(['zotero', 'calibre'])

    expect(s.sources).toEqual(['zotero'])
  })
})

describe('useBrowseStore academic filter', () => {
  it('clears the chosen kinds when the filter goes off', () => {
    const s = store()
    s.toggleAcademic()
    s.toggleAcademicKind('preprint')
    expect(s.academicKinds).toEqual(['preprint'])

    s.toggleAcademic()

    expect(s.academicFilter).toBe(false)
    expect(s.academicKinds).toEqual([])
  })

  it('toggles one kind at a time', () => {
    const s = store()

    s.toggleAcademicKind('paper')
    s.toggleAcademicKind('preprint')
    expect(s.academicKinds).toEqual(['paper', 'preprint'])
    s.toggleAcademicKind('paper')
    expect(s.academicKinds).toEqual(['preprint'])
  })
})

describe('useBrowseStore tag filters', () => {
  it('toggles source, level-1 and level-2 tags independently', () => {
    const s = store()

    s.toggleSourceTag('physics')
    s.toggleLevel1Tag('machine learning')
    s.toggleLevel2Tag('transformers')

    expect(s.sourceTags).toEqual(['physics'])
    expect(s.level1Tags).toEqual(['machine learning'])
    expect(s.level2Tags).toEqual(['transformers'])

    s.toggleLevel1Tag('machine learning')
    expect(s.level1Tags).toEqual([])
    expect(s.sourceTags).toEqual(['physics'])
  })
})

describe('useBrowseStore selection', () => {
  it('starts with nothing selected', () => {
    expect(store().isDocSelected(1)).toBe(false)
  })

  it('toggles one document on and off again', () => {
    const s = store()

    s.toggleDocSelection(7)
    expect(s.isDocSelected(7)).toBe(true)
    s.toggleDocSelection(7)
    expect(s.isDocSelected(7)).toBe(false)
  })

  it('replaces the selection when selecting a whole page', () => {
    const s = store()
    s.toggleDocSelection(1)

    s.selectAllOnPage([2, 3])

    expect(s.isDocSelected(1)).toBe(false)
    expect(s.isDocSelected(2)).toBe(true)
    expect(s.isDocSelected(3)).toBe(true)
  })

  it('clears the selection', () => {
    const s = store()
    s.selectAllOnPage([4, 5])

    s.clearSelection()

    expect(s.isDocSelected(4)).toBe(false)
    expect(s.isDocSelected(5)).toBe(false)
  })
})

describe('useBrowseStore view mode', () => {
  it('defaults to cards', () => {
    expect(store().viewMode).toBe('cards')
  })

  it('persists the chosen mode', () => {
    const s = store()

    s.setViewMode('lines')

    expect(s.viewMode).toBe('lines')
    expect(localStorage.getItem('alexandria-browse-view-mode')).toBe('lines')
  })
})
