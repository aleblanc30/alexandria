<template>
  <div class="domain-lists-grid">
    <section>
      <button
        type="button"
        class="section-toggle"
        :aria-expanded="!collapsed.domains"
        aria-controls="domain-list-top"
        @click="collapsed.domains = !collapsed.domains"
      >
        <span class="chevron" :class="{ collapsed: collapsed.domains }" aria-hidden="true">▾</span>
        <h2 class="section-title">Top domains</h2>
        <span v-if="data" class="hint count">{{ data.top_domains.length }}</span>
      </button>
      <div v-show="!collapsed.domains" id="domain-list-top" class="table-wrap scroll-body">
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
    </section>

    <section>
      <button
        type="button"
        class="section-toggle"
        :aria-expanded="!collapsed.unfetchable"
        aria-controls="domain-list-unfetchable"
        @click="collapsed.unfetchable = !collapsed.unfetchable"
      >
        <span class="chevron" :class="{ collapsed: collapsed.unfetchable }" aria-hidden="true">▾</span>
        <h2 class="section-title">Top unfetchable domains</h2>
        <span v-if="data" class="hint count">{{ data.top_unfetchable.length }}</span>
      </button>
      <div
        v-show="!collapsed.unfetchable"
        id="domain-list-unfetchable"
        class="table-wrap scroll-body"
      >
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
    </section>
  </div>
</template>

<script setup lang="ts">
import { reactive } from 'vue'
import type { DomainTopLists } from '@/api/client'

defineProps<{ data: DomainTopLists | null }>()

const collapsed = reactive({ domains: false, unfetchable: false })
</script>

<style scoped>
.domain-lists-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
  gap: 20px;
  margin-top: 24px;
}
.section-toggle {
  display: flex;
  align-items: baseline;
  gap: 6px;
  width: 100%;
  margin-bottom: 10px;
  padding: 0;
  border: none;
  background: none;
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
}
.section-toggle .section-title { margin: 0 }
.chevron { display: inline-block; font-size: 12px; color: var(--hint); transition: transform .1s }
.chevron.collapsed { transform: rotate(-90deg) }
.count { font-size: 11px }
/* Shows every domain, not only the top few: the body scrolls past ~12 rows. */
.scroll-body { max-height: 420px; overflow-y: auto }
.scroll-body :deep(thead th) {
  position: sticky;
  top: 0;
  z-index: 1;
  background: var(--surface);
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
