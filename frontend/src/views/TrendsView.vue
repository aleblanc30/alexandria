<template>
  <div>
    <h1 class="page-title">Interest trends</h1>
    <p class="page-sub">Level-1 clusters · kernel-smoothed bookmark activity (quarter-wide support), normalized by cluster size</p>

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
      <div class="filter-row" style="margin-bottom: 12px">
        <span class="card-label" style="margin-bottom: 0">Additions rate by cluster</span>
        <span class="chip-sep" />
        <span class="chip" :class="{ active: !stackedView }" @click="stackedView = false">Overlay</span>
        <span class="chip" :class="{ active: stackedView }" @click="stackedView = true">Stacked</span>
      </div>
      <div v-if="timelineReady && selectedClusters.length" class="chart-wrap chart-wrap--trends">
        <Line :data="chartData" :options="chartOptions" />
      </div>
      <p v-else-if="timelineReady" class="hint">Select at least one cluster to show the chart.</p>
    </div>
  </div>
</template>
<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { Line } from 'vue-chartjs'
import { Chart as ChartJS, CategoryScale, LinearScale, LineElement, PointElement, Legend, Tooltip, Filler } from 'chart.js'
import { trendTimeline } from '@/api/client'
import { colorForIndex } from '@/constants/colors'

ChartJS.register(CategoryScale, LinearScale, LineElement, PointElement, Legend, Tooltip, Filler)

const timeline = ref<Record<string, Record<string, number>>>({})
const clusterSizes = ref<Record<string, number>>({})
const timelineReady = ref(false)
const selectedClusters = ref<string[]>([])
const stackedView = ref(false)

onMounted(async () => {
  const res = await trendTimeline()
  timeline.value = res.timeline
  clusterSizes.value = res.sizes
  selectedClusters.value = Object.keys(timeline.value)
  timelineReady.value = true
})

const clusterColor = computed(() => {
  const map: Record<string, string> = {}
  Object.keys(timeline.value).sort().forEach((label, i) => {
    map[label] = colorForIndex(i)
  })
  return map
})

const clusterEntries = computed(() =>
  Object.entries(timeline.value)
    .map(([label]) => ({
      label,
      size: clusterSizes.value[label] ?? 0,
      color: clusterColor.value[label],
    }))
    .sort((a, b) => b.size - a.size),
)

const allMonths = computed(() => {
  const s = new Set<string>()
  Object.values(timeline.value).forEach(m => Object.keys(m).forEach(k => s.add(k)))
  return [...s].sort()
})

function selectAll() {
  selectedClusters.value = Object.keys(timeline.value)
}

function deselectAll() {
  selectedClusters.value = []
}

const selectedSeries = computed(() =>
  clusterEntries.value
    .filter(c => selectedClusters.value.includes(c.label))
    .map(c => ({
      label: c.label,
      data: timeline.value[c.label] ?? {},
      size: c.size,
      color: c.color,
    })),
)

function seriesValue(data: Record<string, number>, size: number, month: string): number {
  return size ? (data[month] ?? 0) / size : 0
}

function stackedCumulativeValues(series: { data: Record<string, number>; size: number }[]): number[][] {
  const incrementals = series.map(({ data, size }) =>
    allMonths.value.map(m => seriesValue(data, size, m)),
  )
  return incrementals.map((_, i) =>
    allMonths.value.map((_, mi) =>
      incrementals.slice(0, i + 1).reduce((sum, values) => sum + values[mi], 0),
    ),
  )
}

const chartData = computed(() => {
  const series = selectedSeries.value
  if (!stackedView.value) {
    return {
      labels: allMonths.value,
      datasets: series.map(({ label, data, size, color }) => ({
        label,
        tension: 0.3,
        pointRadius: 2,
        borderWidth: 2,
        borderColor: color,
        backgroundColor: color + '22',
        fill: false,
        data: allMonths.value.map(m => seriesValue(data, size, m)),
      })),
    }
  }

  const cumulative = stackedCumulativeValues(series)
  return {
    labels: allMonths.value,
    datasets: series.map(({ label, color }, i) => ({
      label,
      tension: 0.3,
      pointRadius: 0,
      borderWidth: 1,
      borderColor: color,
      backgroundColor: color + 'CC',
      fill: i === 0 ? 'origin' : '-1',
      data: cumulative[i],
    })),
  }
})

const chartOptions = computed(() => ({
  responsive: true,
  maintainAspectRatio: false,
  interaction: { mode: 'index' as const, intersect: false },
  plugins: {
    legend: { display: stackedView.value },
    filler: { propagate: false },
    tooltip: {
      callbacks: {
        label(ctx: { datasetIndex: number; dataset: { label?: string }; parsed: { y: number | null }; dataIndex: number }) {
          const month = allMonths.value[ctx.dataIndex]
          const raw = timeline.value[ctx.dataset.label ?? '']?.[month] ?? 0
          let share = ctx.parsed.y ?? 0
          if (stackedView.value) {
            const lower = ctx.datasetIndex === 0
              ? 0
              : Number(chartData.value.datasets[ctx.datasetIndex - 1]?.data[ctx.dataIndex] ?? 0)
            share = share - lower
          }
          const pct = (share * 100).toFixed(1)
          return `${ctx.dataset.label}: ${pct}% (weight ${raw.toFixed(2)})`
        },
        footer(items: { parsed: { y: number | null } }[]) {
          if (!stackedView.value || items.length < 2) return ''
          const total = items[items.length - 1].parsed.y ?? 0
          return `Stack total: ${(total * 100).toFixed(1)}%`
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
      title: {
        display: true,
        text: stackedView.value ? 'Stacked kernel share' : 'Share of cluster',
        font: { size: 11 },
      },
    },
  },
}))
</script>
