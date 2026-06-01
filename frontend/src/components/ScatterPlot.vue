<template>
  <div class="scatter-wrap">
    <canvas
      ref="canvas"
      class="scatter"
      @mousemove="onMove"
      @mouseleave="tooltip = null"
    />
    <div
      v-if="tooltip"
      class="tooltip"
      :style="{ left: tooltip.x + 'px', top: tooltip.y + 'px' }"
    >
      {{ tooltip.title }}
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, watch, onMounted, onUnmounted } from 'vue'
import type { UmapPoint, ClusterOut } from '@/api/client'
import { colorForIndex } from '@/constants/colors'

const props = defineProps<{ points: UmapPoint[]; clusters: ClusterOut[] }>()
const canvas  = ref<HTMLCanvasElement>()
const tooltip = ref<{ x: number; y: number; title: string } | null>(null)

// Component-scoped axis bounds — see audit patch #12.
const bounds = ref({ xMin: 0, xMax: 10, yMin: 0, yMax: 10 })

function clusterColor(cid: number | null) {
  if (cid == null) return '#ccc'
  return colorForIndex(cid)
}

function draw() {
  const c = canvas.value
  if (!c) return
  const ctx = c.getContext('2d')
  if (!ctx) return
  const { width: W, height: H } = c
  ctx.clearRect(0, 0, W, H)

  const pts = props.points
  if (!pts.length) return

  bounds.value = {
    xMin: Math.min(...pts.map(p => p.x)),
    xMax: Math.max(...pts.map(p => p.x)),
    yMin: Math.min(...pts.map(p => p.y)),
    yMax: Math.max(...pts.map(p => p.y)),
  }
  const { xMin, xMax, yMin, yMax } = bounds.value
  const pad = 24

  const sx = (x: number) => pad + ((x - xMin) / (xMax - xMin || 1)) * (W - 2 * pad)
  const sy = (y: number) => H - pad - ((y - yMin) / (yMax - yMin || 1)) * (H - 2 * pad)

  for (const p of pts) {
    ctx.beginPath()
    ctx.arc(sx(p.x), sy(p.y), 3, 0, Math.PI * 2)
    ctx.fillStyle = clusterColor(p.cluster_id) + 'cc'
    ctx.fill()
  }
}

function onMove(e: MouseEvent) {
  const c = canvas.value
  if (!c) return
  const rect = c.getBoundingClientRect()
  const mx = e.clientX - rect.left
  const my = e.clientY - rect.top
  const { xMin, xMax, yMin, yMax } = bounds.value
  const pad = 24
  const { width: W, height: H } = c

  const sx = (x: number) => pad + ((x - xMin) / (xMax - xMin || 1)) * (W - 2 * pad)
  const sy = (y: number) => H - pad - ((y - yMin) / (yMax - yMin || 1)) * (H - 2 * pad)

  for (const p of props.points) {
    const dx = mx - sx(p.x)
    const dy = my - sy(p.y)
    if (Math.sqrt(dx * dx + dy * dy) < 6) {
      tooltip.value = { x: e.offsetX + 12, y: e.offsetY - 8, title: p.title }
      return
    }
  }
  tooltip.value = null
}

let ro: ResizeObserver | undefined
onMounted(() => {
  ro = new ResizeObserver(() => {
    const c = canvas.value
    if (!c) return
    c.width  = c.clientWidth
    c.height = c.clientHeight
    draw()
  })
  if (canvas.value) ro.observe(canvas.value)
})
onUnmounted(() => ro?.disconnect())
watch(() => [props.points, props.clusters], draw, { deep: false })
</script>

<style scoped>
/* Fixed-height wrapper prevents the canvas from inflating its container
   on each render (companion to the patch #12 reactive-bounds fix). */
.scatter-wrap { position: relative; height: 320px; width: 100% }
.scatter      { width: 100%; height: 100%; display: block; cursor: crosshair }
.tooltip      {
  position: absolute;
  background: var(--text);
  color: #fff;
  font-size: 11px;
  padding: 4px 8px;
  border-radius: 4px;
  pointer-events: none;
  max-width: 240px;
  z-index: 20;
}
</style>
