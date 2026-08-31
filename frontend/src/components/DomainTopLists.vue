<template>
  <div class="domain-lists-grid">
    <div>
      <h2 class="section-title">Top domains</h2>
      <div class="table-wrap">
        <table v-if="data && data.top_domains.length" class="tag-table">
          <thead>
            <tr><th>#</th><th>Domain</th><th class="right">Docs</th><th></th></tr>
          </thead>
          <tbody>
            <tr v-for="(row, i) in data.top_domains" :key="row.domain">
              <td class="hint">{{ i + 1 }}</td>
              <td>{{ row.domain }}</td>
              <td class="right">{{ row.count }}</td>
              <td><span v-if="row.has_handler" class="handler-badge">handler</span></td>
            </tr>
          </tbody>
        </table>
        <p v-else class="empty-hint">No HTTP(S) URLs ingested yet.</p>
      </div>
    </div>

    <div>
      <h2 class="section-title">Top unfetchable domains</h2>
      <div class="table-wrap">
        <table v-if="data && data.top_unfetchable.length" class="tag-table">
          <thead>
            <tr><th>#</th><th>Domain</th><th class="right">Failed</th><th></th></tr>
          </thead>
          <tbody>
            <tr v-for="(row, i) in data.top_unfetchable" :key="row.domain">
              <td class="hint">{{ i + 1 }}</td>
              <td>{{ row.domain }}</td>
              <td class="right">{{ row.unfetchable }} / {{ row.count }}</td>
              <td><span v-if="row.has_handler" class="handler-badge">handler</span></td>
            </tr>
          </tbody>
        </table>
        <p v-else class="empty-hint">No fetch failures recorded.</p>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import type { DomainTopLists } from '@/api/client'

defineProps<{ data: DomainTopLists | null }>()
</script>

<style scoped>
.domain-lists-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
  gap: 20px;
  margin-top: 24px;
}
.empty-hint { padding: 14px 12px; font-size: 12px; color: var(--hint) }
.handler-badge {
  font-size: 10px;
  padding: 2px 6px;
  border-radius: 4px;
  background: #EAF3DE;
  color: #3B6D11;
  font-weight: 500;
}
</style>
