import type { DocumentListItem, DocumentOut } from '@/api/client'

export function toGridItem(doc: DocumentOut): DocumentListItem {
  return {
    id: doc.id,
    source: doc.source,
    source_id: doc.source_id,
    title: doc.title,
    description: '',
    url_or_path: doc.url_or_path,
    archive_url: doc.archive_url,
    zotero_attachment_key: doc.zotero_attachment_key,
    source_tags: doc.source_tags,
    cluster_l1_tags: doc.cluster_label ? [doc.cluster_label] : [],
    cluster_l2_tags: [],
  }
}

export function clusterLabel(doc: DocumentOut | DocumentListItem): string | null {
  if ('cluster_label' in doc && doc.cluster_label) return doc.cluster_label
  if ('cluster_l1_tags' in doc && doc.cluster_l1_tags.length) return doc.cluster_l1_tags[0]
  return null
}

export function docYear(doc: DocumentOut | DocumentListItem): string {
  if ('date_added' in doc && doc.date_added) {
    return String(new Date(doc.date_added * 1000).getFullYear())
  }
  return ''
}

export function docSimilarity(doc: DocumentOut | DocumentListItem): number | null {
  if ('similarity' in doc) return doc.similarity
  return null
}
