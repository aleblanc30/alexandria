export type AcademicKind = 'paper' | 'preprint'

export function resolveGeneralTags(academic: boolean, kinds: AcademicKind[]): string[] | undefined {
  if (!academic) return undefined
  if (!kinds.length || (kinds.includes('paper') && kinds.includes('preprint'))) {
    return ['academic']
  }
  return [...kinds]
}

export interface BrowseFilterState {
  sources: string[]
  sourceTags: string[]
  level1Tags: string[]
  level2Tags: string[]
  academicFilter: boolean
  academicKinds: AcademicKind[]
  waybackOnly: boolean
}

/**
 * Build the shared tag/source filter fields used by document list + search
 * requests. ``includeSources`` adds the ``sources`` field (omitted for the
 * tag-scope query, which is already scoped by sources separately).
 */
export function buildDocumentFilters(
  s: BrowseFilterState,
  { includeSources = false }: { includeSources?: boolean } = {},
): Record<string, unknown> {
  const filters: Record<string, unknown> = {
    source_tags: s.sourceTags.length ? s.sourceTags : undefined,
    general_tags: resolveGeneralTags(s.academicFilter, s.academicKinds),
    cluster_l1_tags: s.level1Tags.length ? s.level1Tags : undefined,
    cluster_l2_tags: s.level2Tags.length ? s.level2Tags : undefined,
    wayback_only: s.waybackOnly || undefined,
  }
  if (includeSources) {
    filters.sources = s.sources.length ? s.sources : undefined
  }
  return filters
}
