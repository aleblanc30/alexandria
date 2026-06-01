/**
 * Shared interval-polling timer. Calls ``fn`` immediately on start and then every
 * ``intervalMs`` until stopped. ``start`` is idempotent while a timer is active.
 */
export function usePolling(fn: () => void | Promise<void>, intervalMs: number) {
  let timer: ReturnType<typeof setInterval> | null = null

  function start() {
    if (timer) return
    timer = setInterval(fn, intervalMs)
    void fn()
  }

  function stop() {
    if (timer) {
      clearInterval(timer)
      timer = null
    }
  }

  return { start, stop }
}
