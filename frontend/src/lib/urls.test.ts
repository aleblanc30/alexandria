import { describe, expect, it } from 'vitest'
import {
  hasOpenableUrl,
  isHttpUrl,
  resolveHttpUrl,
  resolveOpenUrl,
} from './urls'

describe('urls', () => {
  it('isHttpUrl detects http and https', () => {
    expect(isHttpUrl('https://example.com')).toBe(true)
    expect(isHttpUrl('ftp://x.com')).toBe(false)
    expect(isHttpUrl(null)).toBe(false)
  })

  it('resolveHttpUrl maps bare DOI for zotero', () => {
    expect(resolveHttpUrl('zotero', '10.1234/abc')).toBe(
      'https://doi.org/10.1234%2Fabc',
    )
  })

  it('resolveOpenUrl prefers archive snapshot', () => {
    expect(
      resolveOpenUrl(
        'firefox',
        'https://dead.example',
        'https://web.archive.org/web/2020/https://dead.example',
      ),
    ).toBe('https://web.archive.org/web/2020/https://dead.example')
  })

  it('hasOpenableUrl is false for filesystem paths', () => {
    expect(hasOpenableUrl('zotero', '/home/user/paper.pdf')).toBe(false)
  })
})
