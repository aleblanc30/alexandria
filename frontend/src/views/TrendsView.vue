<template>
  <div>
    <h1 class="page-title">Interest trends</h1>
    <p class="page-sub">Monthly additions per cluster, normalized by cluster size</p>

    <div v-if="timelineReady" class="cluster-filter mb-4">
      <div class="cluster-filter__toolbar">
        <span class="cluster-filter__title">Clusters</span>
        <button type="button" class="cluster-filter__action" @click="selectAll">Select all</button>
        <span class="hint">·</span>
        <button type="button" class="cluster-filter__action" @click="deselectAll">Deselect all</button>
        <span class="hint cluster-filter__count">{{ selectedClusters.length }} / {{ clusterEntries.length }} shown</span>
      </div>
      <div class="cluster-filter__list">
        <label
          v-for="c in clusterEntries"
          :key="c.label"
          class="cluster-check"
          :class="{ 'cluster-check--off': !selectedClusters.includes(c.label) }"
        >
          <input v-model="selectedClusters" type="checkbox" :value="c.label" />
          <span class="cluster-check__swatch" :style="{ background: c.color }" />
          <span class="cluster-check__name">{{ c.label }}</span>
          <span class="cluster-check__size hint">{{ c.size }}</span>
        </label>
      </div>
    </div>

    <div class="card">
      <div class="card-label">Additions rate by cluster</div>
      <div v-if="timelineReady && selectedClusters.length" class="chart-wrap chart-wrap--trends">
        <Line :data="normalizedData" :options="lineOpts" />
      </div>
      <p v-else-if="timelineReady" class="hint">Select at least one cluster to show the chart.</p>
    </div>
  </div>
</template>
<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { Line } from 'vue-chartjs'
import { Chart as ChartJS, CategoryScale, LinearScale, LineElement, PointElement, Legend, Tooltip } from 'chart.js'
import { trendTimeline } from '@/api/client'

ChartJS.register(CategoryScale, LinearScale, LineElement, PointElement, Legend, Tooltip)

const timeline = ref<Record<string, Record<string, number>>>({})
const timelineReady = ref(false)
const selectedClusters = ref<string[]>([])

const PALETTE = ['#378ADD', '#7F77DD', '#639922', '#BA7517', '#1D9E75', '#D85A30']

onMounted(async () => {
  timeline.value = await trendTimeline('month')
  selectedClusters.value = Object.keys(timeline.value)
  timelineReady.value = true
})

const clusterColor = computed(() => {
  const map: Record<string, string> = {}
  Object.keys(timeline.value).sort().forEach((label, i) => {
    map[label] = PALETTE[i % PALETTE.length]
  })
  return map
})

const clusterEntries = computed(() =>
  Object.entries(timeline.value)
    .map(([label, data]) => ({
      label,
      size: clusterSize(data),
      color: clusterColor.value[label],
    }))
    .sort((a, b) => b.size - a.size),
)

const allMonths = computed(() => {
  const s = new Set<string>()
  Object.values(timeline.value).forEach(m => Object.keys(m).forEach(k => s.add(k)))
  return [...s].sort()
})

function clusterSize(data: Record<string, number>): number {
  return Object.values(data).reduce((a, b) => a + b, 0)
}

function selectAll() {
  selectedClusters.value = Object.keys(timeline.value)
}

function deselectAll() {
  selectedClusters.value = []
}

const normalizedData = computed(() => ({
  labels: allMonths.value,
  datasets: Object.entries(timeline.value)
    .filter(([label]) => selectedClusters.value.includes(label))
    .map(([label, data]) => {
      const size = clusterSize(data)
      const color = clusterColor.value[label]
      return {
        label,
        tension: 0.3,
        pointRadius: 2,
        borderColor: color,
        backgroundColor: color + '22',
        data: allMonths.value.map(m => (size ? (data[m] ?? 0) / size : 0)),
      }
    }),
}))

const lineOpts = {
  responsive: true,
  maintainAspectRatio: false,
  plugins: {
    legend: { display: false },
    tooltip: {
      callbacks: {
        label(ctx: { dataset: { label?: string }; parsed: { y: number }; dataIndex: number }) {
          const pct = (ctx.parsed.y * 100).toFixed(1)
          const raw = timeline.value[ctx.dataset.label ?? '']?.[allMonths.value[ctx.dataIndex]] ?? 0
          return `${ctx.dataset.label}: ${pct}% (${raw} added)`
        },
      },
    },
  },
  scales: {
    x: { ticks: { font: { size: 10 } } },
    y: {
      ticks: {
        font: { size: 10 },
        callback: (v: number | string) => `${(Number(v) * 100).toFixed(0)}%`,
      },
      title: { display: true, text: 'Share of cluster', font: { size: 11 } },
    },
  },
}
</script>
