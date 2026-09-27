import { useToastStore } from '@/stores/toast'

/**
 * Push an error toast from any caught value.
 *
 * Catch clauses hand this an `unknown`, which is what a `throw` can actually
 * produce: an Error, an ApiError, or any value at all from a library that
 * throws a string. The narrowing lives here rather than at each call site so
 * the callers stay a bare `catch (e: unknown)`.
 */
export function notifyError(e: unknown): void {
  useToastStore().push(errorMessage(e), 'error')
}

/** The most useful string available for a caught value. */
export function errorMessage(e: unknown): string {
  if (e instanceof Error) return e.message
  if (typeof e === 'string') return e
  if (e && typeof e === 'object' && 'message' in e) {
    const { message } = e as { message: unknown }
    if (typeof message === 'string') return message
  }
  return String(e)
}
