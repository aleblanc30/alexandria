const HTTP_URL_RE = /^https?:\/\//i
const DOI_PATH_RE = /^10\.\d{4,9}\/\S+$/i
const FILE_PATH_RE = /^([A-Za-z]:\\|\/|storage:)/

export function isHttpUrl(value: string | null | undefined): boolean {
  if (!value) return false
  return HTTP_URL_RE.test(value.trim())
}

function looksLikeFilesystemPath(value: string): boolean {
  return FILE_PATH_RE.test(value.trim())
}

function doiToHttpUrl(value: string): string | null {
  const trimmed = value.trim()
  if (isHttpUrl(trimmed)) return trimmed
  const bare = trimmed.replace(/^doi:\s*/i, '')
  if (DOI_PATH_RE.test(bare)) return `https://doi.org/${encodeURIComponent(bare)}`
  return null
}

/** Browser-openable URL for a document row (Firefox http URLs, Zotero http URL or DOI). */
export function resolveHttpUrl(
  source: string,
  urlOrPath: string | null | undefined,
): string | null {
  if (!urlOrPath) return null
  const value = urlOrPath.trim()
  if (isHttpUrl(value)) return value
  if (source === 'zotero' && !looksLikeFilesystemPath(value)) {
    return doiToHttpUrl(value)
  }
  return null
}

/** URL to open in the browser; prefers a Wayback snapshot when present. */
export function resolveOpenUrl(
  source: string,
  urlOrPath: string | null | undefined,
  archiveUrl?: string | null,
): string | null {
  if (archiveUrl && isHttpUrl(archiveUrl)) return archiveUrl.trim()
  return resolveHttpUrl(source, urlOrPath)
}

export function hasOpenableUrl(
  source: string,
  urlOrPath: string | null | undefined,
  archiveUrl?: string | null,
): boolean {
  return resolveOpenUrl(source, urlOrPath, archiveUrl) != null
}

export function openInNewTab(url: string): void {
  window.open(url, '_blank', 'noopener,noreferrer')
}
