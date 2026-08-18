import { describe, expect, it } from 'vitest'
import {
  INGESTION_SOURCES,
  ingestJobLabel,
  isIngestionSource,
  sourceHasFetchPhase,
  sourceIsExperimental,
  sourceIsPathBased,
  sourceSkipsEmbedPhase,
  visibleSources,
} from './sources'

describe('sources', () => {
  it('covers all backend ingestion sources', () => {
    expect(INGESTION_SOURCES).toEqual(['firefox', 'zotero', 'calibre', 'image', 'youtube', 'reddit'])
  })

  it('isIngestionSource type guard', () => {
    expect(isIngestionSource('zotero')).toBe(true)
    expect(isIngestionSource('unknown')).toBe(false)
  })

  it('firefox has fetch phase and skips embed phase', () => {
    expect(sourceHasFetchPhase('firefox')).toBe(true)
    expect(sourceSkipsEmbedPhase('firefox')).toBe(true)
    expect(sourceHasFetchPhase('zotero')).toBe(false)
  })

  it('reddit fetches link posts but keeps a separate embed phase', () => {
    expect(sourceHasFetchPhase('reddit')).toBe(true)
    expect(sourceSkipsEmbedPhase('reddit')).toBe(false)
  })

  it('reddit is credential-based, not path-based', () => {
    expect(sourceIsPathBased('reddit')).toBe(false)
    expect(sourceIsPathBased('firefox')).toBe(true)
  })

  it('youtube is experimental; reddit is not, since the feed loader works', () => {
    expect(sourceIsExperimental('youtube')).toBe(true)
    expect(sourceIsExperimental('reddit')).toBe(false)
    expect(sourceIsExperimental('firefox')).toBe(false)
  })

  it('visibleSources hides experimental sources unless opted in', () => {
    expect(visibleSources(false)).toEqual(['firefox', 'zotero', 'calibre', 'image', 'reddit'])
    expect(visibleSources(true)).toEqual([...INGESTION_SOURCES])
  })

  it('ingestJobLabel reflects fetch phase', () => {
    expect(ingestJobLabel('firefox')).toBe('Fetch & embed')
    expect(ingestJobLabel('zotero')).toBe('Embed')
  })
})
