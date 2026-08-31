import { describe, expect, it } from 'vitest'
import {
  clusterLabel,
  docAddedYear,
  docDescription,
  docSimilarity,
  toGridItem,
} from './docDisplay'
import type { DocumentOut } from '@/api/client'

const baseDoc: DocumentOut = {
  id: 1,
  source: 'zotero',
  source_id: 'ABC12345',
  title: 'Sample Paper',
  url_or_path: 'https://doi.org/10.1/example',
  archive_url: null,
  zotero_attachment_key: null,
  date_added: 1700000000,
  fetch_status: 'available',
  source_tags: ['ml'],
  overlay_tags: [],
  cluster_id: 2,
  cluster_label: 'Machine Learning',
  similarity: 0.92,
  description: 'A short abstract.',
  note: null,
  doi: null,
  doi_url: null,
  arxiv_id: null,
  isbn: null,
  year: null,
  authors: [],
}

describe('docDisplay', () => {
  it('toGridItem maps cluster label to l1 tags', () => {
    const item = toGridItem(baseDoc)
    expect(item.cluster_l1_tags).toEqual(['Machine Learning'])
    expect(item.description).toBe('A short abstract.')
  })

  it('clusterLabel prefers cluster_label field', () => {
    expect(clusterLabel(baseDoc)).toBe('Machine Learning')
  })

  it('docAddedYear formats from date_added', () => {
    expect(docAddedYear(baseDoc)).toBe('2023')
  })

  it('docSimilarity reads similarity when present', () => {
    expect(docSimilarity(baseDoc)).toBe(0.92)
  })

  it('docDescription returns description or empty string', () => {
    expect(docDescription(baseDoc)).toBe('A short abstract.')
    expect(docDescription({ ...toGridItem(baseDoc) })).toBe('A short abstract.')
  })
})
