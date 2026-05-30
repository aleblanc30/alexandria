// Toast notification store. Messages auto-dismiss after `ttl` ms.
import { defineStore } from 'pinia'
import { ref } from 'vue'

export type ToastLevel = 'info' | 'error'

export interface ToastMessage {
  id: number
  text: string
  level: ToastLevel
}

export const useToastStore = defineStore('toast', () => {
  const messages = ref<ToastMessage[]>([])
  let nextId = 1

  function push(text: string, level: ToastLevel = 'info', ttl = 4000) {
    const id = nextId++
    messages.value.push({ id, text, level })
    setTimeout(() => {
      messages.value = messages.value.filter(m => m.id !== id)
    }, ttl)
  }

  function dismiss(id: number) {
    messages.value = messages.value.filter(m => m.id !== id)
  }

  return { messages, push, dismiss }
})
