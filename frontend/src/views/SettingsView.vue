<template>
  <div>
    <h1 class="page-title">Settings</h1>
    <p class="page-sub">Read-only view of the running configuration — edit via <code>.env</code> / <code>.secrets</code></p>

    <div class="card mb-4" v-if="report">
      <div class="card-label">Capabilities</div>
      <table class="settings-table">
        <thead>
          <tr>
            <th>Capability</th>
            <th>Provider</th>
            <th>Model</th>
            <th>Endpoint</th>
            <th>Credential</th>
            <th>Reachable</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="c in report.capabilities" :key="c.capability">
            <td>{{ CAPABILITY_LABELS[c.capability] ?? c.capability }}</td>
            <td>{{ c.provider }}</td>
            <td>{{ c.model || '—' }}</td>
            <td class="mono">{{ c.base_url || '—' }}</td>
            <td>
              <span v-if="c.base_url === ''" class="hint">n/a</span>
              <span v-else :class="c.credential_present ? 'ok' : 'bad'">
                {{ c.credential_present ? 'set' : 'not set' }}
              </span>
            </td>
            <td>
              <button
                class="btn-xs"
                :disabled="checking === c.capability"
                @click="check(c.capability)"
              >{{ checking === c.capability ? 'Checking…' : 'Check' }}</button>
              <span
                v-if="probes[c.capability]"
                class="probe-result"
                :class="probes[c.capability]!.reachable ? 'ok' : 'bad'"
                :title="probes[c.capability]!.detail"
              >{{ probes[c.capability]!.reachable ? 'reachable' : 'unreachable' }}</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <details v-for="g in report?.groups ?? []" :key="g.name" class="card mb-4 settings-group">
      <summary class="card-label">{{ g.name }}</summary>
      <table class="settings-table">
        <tbody>
          <tr v-for="f in g.fields" :key="f.name" :class="{ 'settings-row--changed': !f.is_default }">
            <td class="mono settings-field-name">{{ f.name }}</td>
            <td class="settings-field-value">
              <span v-if="f.is_secret" :class="f.is_set ? 'ok' : 'hint'">{{ f.is_set ? 'set' : 'not set' }}</span>
              <span v-else class="mono">{{ formatValue(f.value) }}</span>
            </td>
            <td>
              <span v-if="!f.is_default" class="chip chip--changed">changed</span>
            </td>
          </tr>
        </tbody>
      </table>
    </details>

    <p v-if="!report" class="hint">Loading…</p>
  </div>
</template>
<script setup lang="ts">
import { ref, onMounted } from 'vue'
import * as api from '@/api/client'
import type { SettingsReport, ProbeResult } from '@/api/client'

const CAPABILITY_LABELS: Record<string, string> = {
  chat: 'Chat',
  vision: 'Vision',
  gate_vision: 'Image gate',
  ocr: 'OCR',
  image_embed: 'Image embed',
}

const report = ref<SettingsReport | null>(null)
const checking = ref<string | null>(null)
const probes = ref<Record<string, ProbeResult | undefined>>({})

onMounted(async () => {
  report.value = await api.getSettings()
})

async function check(capability: string) {
  checking.value = capability
  try {
    probes.value[capability] = await api.probeCapability(capability)
  } finally {
    checking.value = null
  }
}

function formatValue(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  if (Array.isArray(value)) return value.length ? value.join(', ') : '—'
  if (typeof value === 'boolean') return value ? 'true' : 'false'
  return String(value)
}
</script>
<style scoped>
.settings-table { width: 100%; border-collapse: collapse; font-size: 12px }
.settings-table th { text-align: left; color: var(--hint); font-weight: 500; padding: 4px 8px; border-bottom: 0.5px solid var(--border) }
.settings-table td { padding: 5px 8px; border-bottom: 0.5px solid var(--border) }
.settings-table tr:last-child td { border-bottom: none }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px }
.settings-field-name { color: var(--muted); width: 32% }
.settings-field-value { word-break: break-all }
.settings-row--changed .settings-field-name { color: var(--text); font-weight: 500 }
.ok  { color: #3B6D11 }
.bad { color: #A32D2D }
.probe-result { margin-left: 8px; font-size: 11px }
.chip--changed { background: #E6F1FB; color: #185FA5; border-color: #85B7EB; padding: 1px 8px; font-size: 10px; cursor: default }
.settings-group summary { cursor: pointer }
</style>
