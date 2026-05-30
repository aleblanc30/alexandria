<template>
  <div>
    <div class="page-header">
      <div><h1 class="page-title">Reading lists</h1><p class="page-sub">Curated document collections</p></div>
      <button class="btn btn--primary" @click="createList">+ New list</button>
    </div>

    <div class="rl-layout">
      <div class="rl-nav">
        <div v-for="l in lists" :key="l.list_id"
             class="rl-nav-item" :class="{ active: active === l.list_id }"
             @click="selectList(l.list_id)">
          <span class="rl-name">{{ l.name }}</span>
          <span class="hint">{{ l.item_count }}</span>
        </div>
      </div>

      <div class="rl-content card">
        <template v-if="activeList">
          <div class="rl-header">
            <input v-model="activeList.name" class="rl-title-input" />
            <button class="btn-xs btn-xs--danger" @click="deleteList(activeList.list_id)">Delete</button>
          </div>
          <div v-for="item in items" :key="item.id" class="rl-item-row">
            <div class="rl-item-info">
              <div class="rl-item-title">{{ item.title }}</div>
              <div class="hint">{{ item.source }} · {{ item.note }}</div>
            </div>
            <button class="btn-xs btn-xs--danger" @click="removeItem(item.id)">✕</button>
          </div>
          <p v-if="!items.length" class="hint" style="padding:20px;text-align:center">No documents yet.</p>
        </template>
        <p v-else class="hint" style="padding:24px">Select a list.</p>
      </div>
    </div>
  </div>
</template>
<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import * as api from '@/api/client'
import type { ReadingList, ReadingListItem } from '@/api/client'

const lists      = ref<ReadingList[]>([])
const items      = ref<ReadingListItem[]>([])
const active     = ref<number | null>(null)
const activeList = computed(() => lists.value.find(l => l.list_id === active.value) ?? null)

onMounted(async () => { lists.value = await api.listReadingLists() })

async function selectList(id: number) {
  active.value = id
  items.value  = await api.getListItems(id)
}
async function createList() {
  const r = await api.createReadingList('New list')
  lists.value = await api.listReadingLists()
  selectList(r.list_id)
}
async function deleteList(id: number) {
  await api.deleteReadingList(id)
  lists.value = await api.listReadingLists()
  active.value = null; items.value = []
}
async function removeItem(itemId: number) {
  if (!active.value) return
  await api.removeListItem(active.value, itemId)
  items.value = await api.getListItems(active.value)
  lists.value = await api.listReadingLists()
}
</script>
