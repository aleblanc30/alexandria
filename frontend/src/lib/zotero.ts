const ZOTERO_KEY_RE = /^[A-Z0-9]{8}$/i

export function canOpenInZotero(
  source: string,
  sourceId: string | null | undefined,
): boolean {
  if (source !== 'zotero' || !sourceId) return false
  return ZOTERO_KEY_RE.test(sourceId.trim())
}

/** Open PDF in Zotero reader when attachment key is known; else select parent item. */
export function zoteroOpenUri(
  sourceId: string,
  attachmentKey?: string | null,
): string {
  const attach = attachmentKey?.trim()
  if (attach && ZOTERO_KEY_RE.test(attach)) {
    return `zotero://open-pdf/library/items/${attach}`
  }
  return `zotero://select/library/items/${sourceId.trim()}`
}

export function openInZotero(uri: string): void {
  const anchor = document.createElement('a')
  anchor.href = uri
  anchor.rel = 'noopener'
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
}
