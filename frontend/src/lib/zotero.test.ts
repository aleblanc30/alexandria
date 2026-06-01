import { describe, expect, it, vi } from 'vitest'
import { canOpenInZotero, openInZotero, zoteroOpenUri } from './zotero'

describe('zotero', () => {
  it('canOpenInZotero requires zotero source and 8-char key', () => {
    expect(canOpenInZotero('zotero', 'ABCD1234')).toBe(true)
    expect(canOpenInZotero('firefox', 'ABCD1234')).toBe(false)
    expect(canOpenInZotero('zotero', 'too-long-key')).toBe(false)
  })

  it('zoteroOpenUri opens PDF when attachment key set', () => {
    expect(zoteroOpenUri('PARENT01', 'ATTACH01')).toBe(
      'zotero://open-pdf/library/items/ATTACH01',
    )
    expect(zoteroOpenUri('PARENT01', null)).toBe(
      'zotero://select/library/items/PARENT01',
    )
  })

  it('openInZotero clicks a transient anchor', () => {
    const click = vi.fn()
    const remove = vi.fn()
    const anchor = { href: '', rel: '', click, remove } as unknown as HTMLAnchorElement
    vi.spyOn(document, 'createElement').mockReturnValue(anchor)
    vi.spyOn(document.body, 'appendChild').mockImplementation(() => anchor)
    openInZotero('zotero://select/library/items/TEST0001')
    expect(click).toHaveBeenCalled()
    expect(remove).toHaveBeenCalled()
    vi.restoreAllMocks()
  })
})
