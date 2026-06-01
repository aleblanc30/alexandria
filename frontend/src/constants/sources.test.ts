import { describe, expect, it } from 'vitest'
import {
  INGESTION_SOURCES,
  ingestJobLabel,
  isIngestionSource,
  sourceHasFetchPhase,
  sourceSkipsEmbedPhase,
} from './sources'

describe('sources', () => {
  it('covers all backend ingestion sources', () => {
    expect(INGESTION_SOURCES).toEqual(['firefox', 'zotero', 'calibre', 'image'])
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

  it('ingestJobLabel reflects fetch phase', () => {
    expect(ingestJobLabel('firefox')).toBe('Fetch & embed')
    expect(ingestJobLabel('zotero')).toBe('Embed')
  })
})
