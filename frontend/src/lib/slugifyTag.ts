/** Match backend ``slugify_tag`` for apply-as-tag from cluster label. */
export function slugifyTag(text: string, maxLen = 64): string {
  let s = text.toLowerCase().trim().replace(/[^\w\s-]/g, '')
  s = s.replace(/[\s_]+/g, '-').replace(/-+/g, '-').replace(/^-|-$/g, '')
  return s.slice(0, maxLen)
}
