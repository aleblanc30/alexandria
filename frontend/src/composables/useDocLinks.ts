import { computed } from 'vue'
import { openInNewTab, resolveOpenUrl } from '@/lib/urls'
import { canOpenInZotero } from '@/lib/zotero'

export interface DocLinkSource {
  source: string
  url_or_path: string | null | undefined
  archive_url?: string | null
  source_id: string | null | undefined
}

/** Shared open-in-browser / Zotero link computeds for document cards + panels. */
export function useDocLinks(getDoc: () => DocLinkSource | null | undefined) {
  const openUrl = computed(() => {
    const doc = getDoc()
    return doc ? resolveOpenUrl(doc.source, doc.url_or_path, doc.archive_url) : null
  })
  const hasWebLink = computed(() => openUrl.value != null)
  const canZotero = computed(() => {
    const doc = getDoc()
    return doc != null && canOpenInZotero(doc.source, doc.source_id)
  })

  function openLink() {
    if (openUrl.value) openInNewTab(openUrl.value)
  }

  return { openUrl, hasWebLink, canZotero, openLink }
}
