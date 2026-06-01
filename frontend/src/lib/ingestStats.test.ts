import { describe, expect, it } from 'vitest'
import { formatJobToast, ingestStatsSummary } from './ingestStats'
import type { IngestionStatus, SyncProgress } from '@/api/client'

describe('ingestStats', () => {
  it('formatJobToast summarizes firefox ingest', () => {
    const p = {
      active_job: 'ingest',
      status: 'done',
      last_result: {
        fetch: { fetched: 3, unfetchable: 1, skipped: 0 },
        embed: { processed: 2, skipped: 0, failed: 0 },
      },
    } as unknown as SyncProgress
    const msg = formatJobToast('firefox', p)
    expect(msg).toContain('3 fetched')
    expect(msg).toContain('2 embedded')
  })

  it('ingestStatsSummary for firefox lists fetch stats', () => {
    const st: IngestionStatus = {
      total: 10,
      by_source: {},
      unfetchable: 0,
      pending: 0,
      fetch_by_source: {
        firefox: {
          fetched: 5,
          unfetchable: 1,
          skipped: 0,
          embedded: 4,
          pending: 2,
        },
      },
    }
    const summary = ingestStatsSummary('firefox', st)
    expect(summary).toContain('5 fetched')
    expect(summary).toContain('2 pending')
  })

  it('ingestStatsSummary returns null when no stats', () => {
    expect(ingestStatsSummary('image', null)).toBeNull()
  })
})
