// Typed API client — one function per endpoint.
// All paths are relative so the Vite proxy routes them to FastAPI in dev,
// and FastAPI serves them directly from /dist in production.

const BASE = ''
const DEFAULT_TIMEOUT_MS = 30000
const INGESTION_TIMEOUT_MS = 120000
/** One Ollama call per doc; default batch 20 × ~60s + slack (must exceed DEFAULT_TIMEOUT_MS). */
const PSEUDO_LABEL_LLM_PER_DOC_MS = 65_000
const PSEUDO_LABEL_LLM_BASE_MS = 15_000
const PSEUDO_LABEL_LLM_MAX_MS = 30 * 60 * 1000

function pseudoLabelLlmTimeoutMs(batchSize?: number): number {
  const n = batchSize ?? 20
  return Math.min(
    PSEUDO_LABEL_LLM_MAX_MS,
    PSEUDO_LABEL_LLM_BASE_MS + n * PSEUDO_LABEL_LLM_PER_DOC_MS,
  )
}

export class ApiError extends Error {
  status: number
  constructor(msg: string, status: number) {
    super(msg)
    this.status = status
    this.name = 'ApiError'
  }
}

async function req<T>(
  path: string,
  options: RequestInit = {},
  timeoutMs: number = DEFAULT_TIMEOUT_MS,
): Promise<T> {
  const ctrl = new AbortController()
  const timer = timeoutMs > 0
    ? setTimeout(() => ctrl.abort(), timeoutMs)
    : null
  try {
    const r = await fetch(BASE + path, {
      headers: { 'Content-Type': 'application/json' },
      signal: ctrl.signal,
      ...options,
    })
    if (!r.ok) {
      let detail = r.statusText
      try { detail = (await r.json()).detail ?? detail } catch { /* keep statusText */ }
      throw new ApiError(`${r.status} ${detail} — ${path}`, r.status)
    }
    if (r.status === 204) return undefined as T
    return r.json()
  } catch (e: any) {
    if (e.name === 'AbortError') throw new ApiError(`Timeout: ${path}`, 0)
    throw e
  } finally {
    if (timer) clearTimeout(timer)
  }
}

// ── Types ─────────────────────────────────────────────────────────────────────

export interface TagOut       { tag: string; origin: string; confidence: number | null }
export interface DocumentOut  {
  id: number; source: string; source_id: string; title: string
  url_or_path: string | null; archive_url: string | null
  zotero_attachment_key: string | null
  date_added: number | null; fetch_status: string
  source_tags: string[]; overlay_tags: TagOut[]
  cluster_id: number | null; cluster_label: string | null
  similarity: number | null
  description: string
  note: string | null
  // Structured bibliographic fields (DESIGN.md §3.2).
  doi: string | null
  /** https://doi.org/<doi>, computed by the backend. */
  doi_url: string | null
  arxiv_id: string | null
  isbn: string | null
  /** Publication year — distinct from date_added ("when saved"). */
  year: number | null
  authors: string[]
}
export interface ImageDetail { image_type: string | null; ocr_text: string | null }
export type RedditKind = 'post' | 'comment'
/** Reddit-only fields for a saved post or comment. Null for every other source. */
export interface RedditDetail {
  kind: RedditKind | null
  /** Display name with no `r/` prefix. */
  subreddit: string | null
  /** The reddit thread. Differs from `url_or_path` on a link post. */
  permalink: string | null
  /** Off-reddit target of a link post, else null. */
  external_url: string | null
  /** Post/comment text verbatim — unlike `description`, which is truncated. */
  body: string | null
}
export type EnrichmentKind     = 'summary' | 'external_synopsis'
export type EnrichmentResolver = 'isbn' | 'search' | 'google_books' | 'brave' | 'local_model'
/** One rung of the enrichment cascade that produced generated text (DESIGN.md §3.2). */
export interface Enrichment {
  kind: EnrichmentKind
  /** Null when the backend could not name the rung. */
  resolved_by: EnrichmentResolver | null
  /** Human-readable, already localised by the backend — render as-is. */
  label: string
  source_ref: string | null
  ref_title: string | null
  text: string
}
export interface DocumentDetail extends DocumentOut {
  description: string
  chunks_count: number; collections: string[]
  image: ImageDetail | null
  reddit: RedditDetail | null
  enrichment: Enrichment[]
}
export interface DocumentListItem {
  id: number; source: string; source_id: string; title: string; description: string
  url_or_path: string | null; archive_url: string | null
  zotero_attachment_key: string | null
  source_tags: string[]
  cluster_l1_tags: string[]
  cluster_l2_tags: string[]
}
export interface DocumentListResponse { total: number; documents: DocumentListItem[] }
export interface SearchRequest {
  query: string; sources?: string[]; source_tags?: string[]
  general_tags?: string[]
  cluster_l1_tags?: string[]; cluster_l2_tags?: string[]; wayback_only?: boolean
  cluster_ids?: number[]; tags?: string[]
  date_from?: number; date_to?: number; fetch_status?: string
  mode?: 'semantic' | 'fulltext' | 'hybrid'; limit?: number; offset?: number
}
export interface ImageOut {
  id: number; path: string; filename: string; image_type: string | null
  width: number | null; height: number | null
  description: string | null; ocr_text: string | null
  date_taken: number | null; tags: string[]; similarity: number | null
  // Which search path found this hit; null outside search results.
  matched_by?: 'clip' | 'text' | 'clip+text' | null
}
export interface SearchResponse { query: string; total: number; documents: DocumentOut[] }
export interface ClusterOut    {
  cluster_id: number; label: string; description: string | null; run_id: number; doc_count: number
  level: number; parent_cluster_id: number | null; parent_label: string | null
}
export interface ClusterDetail extends ClusterOut { top_tags: string[] }
export interface ClusterPatchRequest { label: string; description?: string | null }
export interface ApplyTagResult { cluster_id: number; tag: string; applied: number; skipped: number }
export interface ApplyAllTagsResult { clusters: ApplyTagResult[]; total_applied: number; total_skipped: number }
export interface UmapPoint     { doc_id: number; x: number; y: number; cluster_id: number | null; title: string }
export interface RunOut        { run_id: number; timestamp: number; algorithm: string; parameters: Record<string,unknown>; accepted: boolean; status: string; n_clusters: number; n_noise: number; notes: string | null }
export interface ClusterRunParams {
  cluster_space?: 'pca' | 'legacy_umap' | null
  min_cluster_size?: number | null
  min_samples?: number | null
  n_neighbors?: number | null
  min_dist?: number
  pca_components?: number | null
  n_components?: number
  skip_labelling?: boolean
  async_labelling?: boolean
}
export interface DiagnosticsOut { run_id: number; n_clusters: number; n_noise: number; cluster_sizes: Record<string,number>; drift_flags: DriftFlag[]; merge_suggestions: MergeSuggestion[] }
export interface DriftFlag     { cluster_id: number; label: string; drift_score: number; n_recent: number; flagged: boolean }
export interface MergeSuggestion { cluster_id_a: number; label_a: string; cluster_id_b: number; label_b: string; similarity: number }
export interface TagRow        { tag: string; origin: string; count: number }
export interface IngestionStatus {
  total: number
  by_source: Record<string, number>
  pending_metadata_by_source?: Record<string, number>
  fetch_by_source?: Record<string, Record<string, number>>
  source_unavailable?: Record<string, string | null>
  unfetchable: number
  pending: number
}
export interface PhaseBreakdown {
  success: number
  failure: number
  pending: number
}
export interface PhaseDetail {
  name: string
  total: number
  processed: number
  percent: number
  active: boolean
  breakdown?: PhaseBreakdown
}
export interface SyncProgress {
  source: string
  status: 'idle' | 'running' | 'done' | 'error' | 'paused' | 'cancelled'
  phase: string
  active_job: 'metadata' | 'ingest' | null
  total: number
  processed: number
  failed: number
  percent: number
  overall_total: number
  overall_processed: number
  phase_index: number
  phase_count: number
  phases: string[]
  phase_details: PhaseDetail[]
  error: string | null
  last_result?: Record<string, unknown> | null
}
export interface SourceCounts {
  pending_metadata: number
  fetch: Record<string, number>
  unavailable: string | null
}
/** One frame of ``GET /ingestion/sync/events``. */
export interface SyncEvent {
  progress: SyncProgress
  counts: SourceCounts
}
export interface UnfetchableRow  { id: number; title: string; url: string; http_status: number | null; error: string | null; timestamp: number }
export interface DomainRow { domain: string; count: number; unfetchable: number; has_handler: boolean; by_fetch_status: Record<string, number> }
export interface DomainTopLists { top_domains: DomainRow[]; top_unfetchable: DomainRow[] }
export interface ReadingList   { list_id: number; name: string; description: string; created_at: number; item_count: number }
export interface ReadingListItem { id: number; position: number; note: string; doc_id: number; title: string; source: string; url_or_path: string | null }

// ── Search ────────────────────────────────────────────────────────────────────

export const search = (body: SearchRequest) =>
  req<SearchResponse>('/search', { method: 'POST', body: JSON.stringify(body) })

// ── Documents ─────────────────────────────────────────────────────────────────

export const getDocument = async (id: number): Promise<DocumentDetail> => {
  const doc = await req<DocumentDetail>(`/documents/${id}`)
  // Older backends omit `enrichment`; keep it an array so callers can iterate freely.
  return Array.isArray(doc.enrichment) ? doc : { ...doc, enrichment: [] }
}

export const listDocuments = (params?: {
  sources?: string[]
  source_tags?: string[]
  general_tags?: string[]
  overlay_tags?: string[]
  cluster_l1_tags?: string[]
  cluster_l2_tags?: string[]
  learned_tags?: string[]
  wayback_only?: boolean
  limit?: number
  offset?: number
}) => {
  const qs = new URLSearchParams()
  params?.sources?.forEach(s => qs.append('sources', s))
  params?.source_tags?.forEach(t => qs.append('source_tags', t))
  params?.general_tags?.forEach(t => qs.append('general_tags', t))
  params?.overlay_tags?.forEach(t => qs.append('overlay_tags', t))
  params?.cluster_l1_tags?.forEach(t => qs.append('cluster_l1_tags', t))
  params?.cluster_l2_tags?.forEach(t => qs.append('cluster_l2_tags', t))
  params?.learned_tags?.forEach(t => qs.append('learned_tags', t))
  if (params?.wayback_only) qs.set('wayback_only', 'true')
  if (params?.limit != null) qs.set('limit', String(params.limit))
  if (params?.offset != null) qs.set('offset', String(params.offset))
  const query = qs.toString()
  return req<DocumentListResponse>(`/documents${query ? '?' + query : ''}`)
}

export const patchTags = (id: number, add: string[], remove: string[]) =>
  req<void>(`/documents/${id}/tags`, { method: 'PATCH', body: JSON.stringify({ add, remove }) })

// ── Clusters ──────────────────────────────────────────────────────────────────

export const listClusters    = ()  => req<ClusterOut[]>('/clusters')
export const getCluster      = (id: number) => req<ClusterDetail>(`/clusters/${id}`)
export const clusterDocs     = (id: number, limit = 20, offset = 0) =>
  req<DocumentOut[]>(`/clusters/${id}/documents?limit=${limit}&offset=${offset}`)
export const scatterPoints   = ()  => req<UmapPoint[]>('/clusters/scatter/points')
export const applyClusterTag = (id: number, tag?: string) =>
  req<ApplyTagResult>(`/clusters/${id}/apply-tag`, {
    method: 'POST',
    body: JSON.stringify(tag ? { tag } : {}),
  })
export const patchCluster = (id: number, body: ClusterPatchRequest) =>
  req<ClusterOut>(`/clusters/${id}`, { method: 'PATCH', body: JSON.stringify(body) })
export const regenerateClusterLabel = (id: number) =>
  req<ClusterOut>(`/clusters/${id}/regenerate-label`, { method: 'POST', body: '{}' })
export const applyAllClusterTags = () =>
  req<ApplyAllTagsResult>('/clusters/apply-all-tags', { method: 'POST', body: '{}' })

// ── Runs ──────────────────────────────────────────────────────────────────────

export const listRuns        = ()  => req<RunOut[]>('/runs')
export const getDiagnostics  = (id: number) => req<DiagnosticsOut>(`/runs/${id}/diagnostics`)
export const acceptRun       = (id: number) => req<void>(`/runs/${id}/accept`, { method: 'POST' })
export const rejectRun       = (id: number, notes = '') =>
  req<void>(`/runs/${id}/reject?notes=${encodeURIComponent(notes)}`, { method: 'POST' })
export const triggerRun      = (params?: ClusterRunParams) =>
  req<{ status: string; run_id: number }>('/runs/trigger', { method: 'POST', body: JSON.stringify(params ?? {}) })
export const cancelRun       = (id: number) =>
  req<{ status: string; run_id: number }>(`/runs/${id}/cancel`, { method: 'POST' })
export const deleteRun       = (id: number, force = false) =>
  req<void>(`/runs/${id}${force ? '?force=true' : ''}`, { method: 'DELETE' })

// ── Tags ──────────────────────────────────────────────────────────────────────

export const listTags = (params?: {
  origin?: string
  sources?: string[]
  source_tags?: string[]
  cluster_l1_tags?: string[]
  cluster_l2_tags?: string[]
  wayback_only?: boolean
  q?: string
  limit?: number
}) => {
  const qs = new URLSearchParams()
  if (params?.origin) qs.set('origin', params.origin)
  params?.sources?.forEach(s => qs.append('sources', s))
  params?.source_tags?.forEach(t => qs.append('source_tags', t))
  params?.cluster_l1_tags?.forEach(t => qs.append('cluster_l1_tags', t))
  params?.cluster_l2_tags?.forEach(t => qs.append('cluster_l2_tags', t))
  if (params?.wayback_only) qs.set('wayback_only', 'true')
  if (params?.q) qs.set('q', params.q)
  if (params?.limit != null) qs.set('limit', String(params.limit))
  const query = qs.toString()
  return req<TagRow[]>(`/tags${query ? '?' + query : ''}`)
}

// ── Trends ────────────────────────────────────────────────────────────────────

export interface TrendTimelineResponse {
  timeline: Record<string, Record<string, number>>
  sizes: Record<string, number>
}

export const trendTimeline = () =>
  req<TrendTimelineResponse>('/trends/timeline')
export const trendSources  = (granularity: 'month' | 'year' = 'month') =>
  req<Record<string, Record<string, number>>>(`/trends/sources?granularity=${granularity}`)

// ── Ingestion ─────────────────────────────────────────────────────────────────

export const ingestionStatus    = () =>
  req<IngestionStatus>('/ingestion/status', {}, INGESTION_TIMEOUT_MS)
export const syncProgress       = (source?: string) =>
  req<Record<string, SyncProgress>>(
    `/ingestion/sync/progress${source ? `?source=${source}` : ''}`,
    {},
    0,
  )
/**
 * Live progress for one source over SSE. The server closes the stream once the
 * job reaches a terminal state, so ``onEvent`` sees that last frame and the
 * caller closes the source to stop EventSource from reconnecting.
 */
export function syncEvents(
  source: string,
  onEvent: (ev: SyncEvent) => void,
  onError?: () => void,
): EventSource {
  const es = new EventSource(`${BASE}/ingestion/sync/events?source=${encodeURIComponent(source)}`)
  es.onmessage = (msg) => {
    try { onEvent(JSON.parse(msg.data) as SyncEvent) } catch { /* ignore malformed frame */ }
  }
  es.onerror = () => { onError?.() }
  return es
}
export const unfetchableUrls    = (limit = 50, offset = 0) =>
  req<UnfetchableRow[]>(
    `/ingestion/unfetchable?limit=${limit}&offset=${offset}`,
    {},
    INGESTION_TIMEOUT_MS,
  )
export const domainTopLists     = (limit = 10, source?: string) =>
  req<DomainTopLists>(
    `/ingestion/domains?limit=${limit}${source ? `&source=${encodeURIComponent(source)}` : ''}`,
    {},
    INGESTION_TIMEOUT_MS,
  )
export const syncSource         = (source: string, force = false) =>
  req<{ status: string; source: string }>(
    `/ingestion/sync/${source}${force ? '?force=1' : ''}`,
    { method: 'POST' },
    10000,
  )
/** Re-walk a source's whole remote history rather than only what is new.
 *  Only sources in the backend's BACKFILL_SOURCES accept this (reddit today);
 *  anything else answers 400. */
export const backfillMetadata   = (source: string, force = false) =>
  req<{ status: string; backfill: boolean }>(
    `/ingestion/sync/${source}/metadata?backfill=1${force ? '&force=1' : ''}`,
    { method: 'POST' },
  )
export const syncMetadata       = (source: string, force = false) =>
  req<{ status: string; source: string; job: string }>(
    `/ingestion/sync/${source}/metadata${force ? '?force=1' : ''}`,
    { method: 'POST' },
    10000,
  )
export const syncIngest         = (source: string, force = false) =>
  req<{ status: string; source: string; job: string }>(
    `/ingestion/sync/${source}/ingest${force ? '?force=1' : ''}`,
    { method: 'POST' },
    10000,
  )
export const pauseSync          = (source: string) =>
  req<{ status: string; source: string }>(
    `/ingestion/sync/${source}/pause`,
    { method: 'POST' },
    10000,
  )
export const cancelSync         = (source: string) =>
  req<{ status: string; source: string }>(
    `/ingestion/sync/${source}/cancel`,
    { method: 'POST' },
    10000,
  )

export interface PurgeResult { status: string; source: string; counts: Record<string, number> }
export const purgeSource        = (source: string) =>
  req<PurgeResult>(
    `/ingestion/sources/${source}/purge`,
    { method: 'POST' },
    INGESTION_TIMEOUT_MS,
  )

export interface SourcePathInfo { source: string; path: string; kind: 'file' | 'dir'; exists: boolean }

export const getSourcePath = (source: string) =>
  req<SourcePathInfo>(`/ingestion/sources/${source}/path`)
export const setSourcePath = (source: string, path: string) =>
  req<SourcePathInfo>(
    `/ingestion/sources/${source}/path`,
    { method: 'PUT', body: JSON.stringify({ path }) },
  )
// Matches PICKER_TIMEOUT_SECONDS in pka/api/source_paths.py, plus slack so the
// backend's own timeout fires first and returns a real error.
const BROWSE_TIMEOUT_MS = 605_000
export const browseSourcePath = (source: string) =>
  req<{ path: string | null }>(`/ingestion/sources/${source}/browse`, { method: 'POST' }, BROWSE_TIMEOUT_MS)

// ── Image folders (the image source is a list of folders) ──────────────────────
export interface ImageDir { path: string; exists: boolean }

export const getImageDirs = () =>
  req<{ dirs: ImageDir[] }>('/ingestion/sources/image/dirs').then(r => r.dirs)
export const addImageDir = (path: string) =>
  req<{ dirs: ImageDir[] }>(
    '/ingestion/sources/image/dirs',
    { method: 'POST', body: JSON.stringify({ path }) },
  ).then(r => r.dirs)
export const removeImageDir = (path: string) =>
  req<{ dirs: ImageDir[] }>(
    '/ingestion/sources/image/dirs',
    { method: 'DELETE', body: JSON.stringify({ path }) },
  ).then(r => r.dirs)
export const browseImageDir = () =>
  req<{ path: string | null }>(
    '/ingestion/sources/image/dirs/browse',
    { method: 'POST' },
    BROWSE_TIMEOUT_MS,
  )

// ── Images ────────────────────────────────────────────────────────────────────

export const listImages   = (params?: { image_type?: string; limit?: number; offset?: number }) => {
  const qs = new URLSearchParams(params as Record<string,string>).toString()
  return req<ImageOut[]>(`/images${qs ? '?' + qs : ''}`)
}
export const searchImages = (q: string, n = 10, mode: 'hybrid' | 'clip' | 'text' = 'hybrid') =>
  req<ImageOut[]>(`/images/search?q=${encodeURIComponent(q)}&n=${n}&mode=${mode}`)
export const getImage     = (id: number) => req<ImageOut>(`/images/${id}`)

// ── Reading lists ─────────────────────────────────────────────────────────────

export const listReadingLists = () => req<ReadingList[]>('/reading-lists')
export const createReadingList = (name: string, description = '') =>
  req<{ list_id: number }>('/reading-lists', { method: 'POST', body: JSON.stringify({ name, description }) })
export const deleteReadingList = (id: number) =>
  req<void>(`/reading-lists/${id}`, { method: 'DELETE' })
export const getListItems  = (id: number) => req<ReadingListItem[]>(`/reading-lists/${id}/items`)
export const addListItem   = (listId: number, document_id: number, note = '') =>
  req<{ id: number }>(`/reading-lists/${listId}/items`, { method: 'POST', body: JSON.stringify({ document_id, note }) })
export const removeListItem = (listId: number, itemId: number) =>
  req<void>(`/reading-lists/${listId}/items/${itemId}`, { method: 'DELETE' })

// ── Tag training (active learning) ───────────────────────────────────────────

export interface TagTrainingLabel { doc_id: number; label: number }
export interface TagTrainingSession {
  session_id: number
  tag: string
  status: string
  created_at: number
  accepted_at: number | null
  parameters: Record<string, unknown>
  provenance: Record<string, unknown> | null
  positive_count: number
  negative_count: number
  has_model: boolean
  train_stats: Record<string, unknown> | null
  bootstrap_negatives_added?: number
  pseudo_label_result?: {
    mode: string
    added_positive: number
    added_negative: number
    pseudo_label_high?: number
    pseudo_label_low?: number
    errors?: number
    batch_size?: number
  }
}
export interface TagTrainingQueueDoc {
  doc_id: number
  title: string
  probability: number
  uncertainty: number
}

export const listTagTrainingSessions = () =>
  req<TagTrainingSession[]>('/tag-training/sessions')
export const getTagTrainingSession = (id: number) =>
  req<TagTrainingSession>(`/tag-training/sessions/${id}`)
export const createTagTrainingSession = (tag: string, labels: TagTrainingLabel[]) =>
  req<TagTrainingSession>('/tag-training/sessions', {
    method: 'POST',
    body: JSON.stringify({ tag, labels }),
  })
export const createTagTrainingFromSourceTag = (sourceTag: string, targetTag: string) =>
  req<TagTrainingSession>('/tag-training/sessions/from-source-tag', {
    method: 'POST',
    body: JSON.stringify({ source_tag: sourceTag, target_tag: targetTag }),
  })
export const getTagTrainingQueue = (sessionId: number) =>
  req<TagTrainingQueueDoc[]>(`/tag-training/sessions/${sessionId}/queue`)
export const postTagTrainingLabels = (sessionId: number, labels: TagTrainingLabel[]) =>
  req<TagTrainingSession>(`/tag-training/sessions/${sessionId}/labels`, {
    method: 'POST',
    body: JSON.stringify({ labels }),
  })
export const acceptTagTrainingSession = (sessionId: number) =>
  req<TagTrainingSession>(`/tag-training/sessions/${sessionId}/accept`, { method: 'POST' })
export const resumeTagTrainingSession = (sessionId: number) =>
  req<TagTrainingSession>(`/tag-training/sessions/${sessionId}/resume`, { method: 'POST' })
export const pseudoLabelTagTrainingSession = (
  sessionId: number,
  mode: 'model' | 'llm',
  batchSize?: number,
) =>
  req<TagTrainingSession>(
    `/tag-training/sessions/${sessionId}/pseudo-label`,
    {
      method: 'POST',
      body: JSON.stringify({ mode, batch_size: batchSize ?? null }),
    },
    mode === 'llm' ? pseudoLabelLlmTimeoutMs(batchSize) : DEFAULT_TIMEOUT_MS,
  )
export const getTagTrainingSessionByTag = (tag: string) =>
  req<TagTrainingSession>(`/tag-training/sessions/by-tag/${encodeURIComponent(tag)}`)

// ── Settings ──────────────────────────────────────────────────────────────────

export interface SettingField {
  name: string
  value: unknown
  is_secret: boolean
  is_set: boolean | null
  is_default: boolean
  tier: 'install_time' | 'operational' | 'tuning'
}
export interface SettingGroup { name: string; fields: SettingField[] }
export interface CapabilityStatus {
  capability: string
  provider: string
  model: string
  base_url: string
  credential_present: boolean
}
export interface SettingsReport { groups: SettingGroup[]; capabilities: CapabilityStatus[] }
export interface ProbeResult { reachable: boolean; detail: string }

export const getSettings = () => req<SettingsReport>('/settings')
export const probeCapability = (capability: string) =>
  req<ProbeResult>(`/settings/probe/${capability}`, { method: 'POST' })
