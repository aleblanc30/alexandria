import { useToastStore } from '@/stores/toast'

/** Push an error toast from any caught value. */
export function notifyError(e: any): void {
  useToastStore().push(e?.message ?? String(e), 'error')
}
