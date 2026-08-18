import { describe, expect, it } from 'vitest'
import { RUNG_TRUST, rungClass, rungHint, rungKey, rungRank } from './enrichment'

describe('rungKey', () => {
  it('passes through every known rung', () => {
    for (const k of RUNG_TRUST) expect(rungKey(k)).toBe(k)
  })

  it.each([null, undefined, '', 'nonsense'])('falls back to unknown for %p', (v) => {
    expect(rungKey(v as string | null)).toBe('unknown')
  })
})

describe('rungHint', () => {
  it('calls an ISBN match exact', () => {
    expect(rungHint('isbn')).toMatch(/exact/i)
  })

  it('warns that brave is the weakest rung', () => {
    expect(rungHint('brave')).toMatch(/weakest/i)
  })

  it('says a local summary is not an external claim', () => {
    expect(rungHint('local_model')).toMatch(/not an external claim/i)
  })

  it('never returns an empty hint, even for an unrecognised rung', () => {
    for (const v of [null, 'nonsense', 'isbn']) {
      expect(rungHint(v).length).toBeGreaterThan(0)
    }
  })
})

describe('rungClass', () => {
  it('matches the .rung--* style hooks', () => {
    expect(rungClass('isbn')).toBe('rung--isbn')
    expect(rungClass('google_books')).toBe('rung--google_books')
  })

  it('never emits "rung--null" for a missing resolver', () => {
    expect(rungClass(null)).toBe('rung--unknown')
  })
})

describe('rungRank', () => {
  it('orders identifier match above title match above web snippet', () => {
    expect(rungRank('isbn')).toBeLessThan(rungRank('search'))
    expect(rungRank('search')).toBeLessThan(rungRank('brave'))
  })

  it('sorts an unrecognised rung last', () => {
    expect(rungRank('nonsense')).toBe(RUNG_TRUST.length - 1)
  })
})
