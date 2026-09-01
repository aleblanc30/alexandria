import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, domainTopLists, search, triggerRun } from './client'

describe('api client', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn())
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('ApiError carries HTTP status', () => {
    const err = new ApiError('404 Not Found', 404)
    expect(err.status).toBe(404)
    expect(err.name).toBe('ApiError')
  })

  it('search throws ApiError on non-OK response', async () => {
    vi.mocked(fetch).mockResolvedValue({
      ok: false,
      status: 422,
      statusText: 'Unprocessable',
      json: async () => ({ detail: 'bad query' }),
    } as Response)
    await expect(search({ query: 'test' })).rejects.toMatchObject({ status: 422 })
  })

  it('search returns parsed JSON on success', async () => {
    vi.mocked(fetch).mockResolvedValue({
      ok: true,
      status: 200,
      statusText: 'OK',
      json: async () => ({ query: 'hi', total: 0, documents: [], images: [] }),
    } as Response)
    const data = await search({ query: 'hi' })
    expect(data.total).toBe(0)
    expect(fetch).toHaveBeenCalledWith(
      '/search',
      expect.objectContaining({ method: 'POST' }),
    )
  })

  it('domainTopLists builds URL without source', async () => {
    vi.mocked(fetch).mockResolvedValue({
      ok: true,
      status: 200,
      statusText: 'OK',
      json: async () => ({ top_domains: [], top_unfetchable: [] }),
    } as Response)
    await domainTopLists()
    expect(fetch).toHaveBeenCalledWith(
      '/ingestion/domains?limit=10',
      expect.anything(),
    )
  })

  it('domainTopLists includes source when given', async () => {
    vi.mocked(fetch).mockResolvedValue({
      ok: true,
      status: 200,
      statusText: 'OK',
      json: async () => ({ top_domains: [], top_unfetchable: [] }),
    } as Response)
    await domainTopLists(5, 'firefox')
    expect(fetch).toHaveBeenCalledWith(
      '/ingestion/domains?limit=5&source=firefox',
      expect.anything(),
    )
  })

  it('triggerRun sends an empty body when called bare', async () => {
    vi.mocked(fetch).mockResolvedValue({
      ok: true,
      status: 202,
      statusText: 'Accepted',
      json: async () => ({ status: 'queued', run_id: 1 }),
    } as Response)
    await triggerRun()
    expect(fetch).toHaveBeenCalledWith(
      '/runs/trigger',
      expect.objectContaining({ method: 'POST', body: '{}' }),
    )
  })

  it('triggerRun serialises the given params', async () => {
    vi.mocked(fetch).mockResolvedValue({
      ok: true,
      status: 202,
      statusText: 'Accepted',
      json: async () => ({ status: 'queued', run_id: 1 }),
    } as Response)
    await triggerRun({ cluster_space: 'legacy_umap', min_cluster_size: null, min_dist: 0.2 })
    expect(fetch).toHaveBeenCalledWith(
      '/runs/trigger',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ cluster_space: 'legacy_umap', min_cluster_size: null, min_dist: 0.2 }),
      }),
    )
  })
})
