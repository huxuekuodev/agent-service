<script setup>
import { computed } from 'vue'

const props = defineProps({
  data: { type: Object, default: null }, // monitorQuery 的返回：{series:[{bucket,value,count,model}], total}
  height: { type: Number, default: 160 },
})

const W = 640
const H = 200
const PAD = 8

const PALETTE = ['#4f6ef2', '#e5484d', '#1a7f4e', '#e08a00', '#7c5ce0', '#00a2c8', '#9c6bde', '#d3489a']

const lines = computed(() => {
  const d = props.data
  if (!d || !Array.isArray(d.series) || !d.series.length) return []
  const series = d.series.filter((s) => s.value !== null && s.value !== undefined)
  if (!series.length) return []

  const buckets = [...new Set(series.map((s) => s.bucket))].sort()
  const values = series.map((s) => s.value)
  const min = Math.min(...values)
  const max = Math.max(...values)
  const span = max - min || 1

  const xOf = (bucket) => {
    const idx = buckets.indexOf(bucket)
    return buckets.length === 1 ? W / 2 : PAD + (idx / (buckets.length - 1)) * (W - PAD * 2)
  }
  const yOf = (v) => H - PAD - ((v - min) / span) * (H - PAD * 2)

  // 按 model 分组
  const byModel = new Map()
  for (const s of series) {
    const key = s.model || '__all__'
    if (!byModel.has(key)) byModel.set(key, [])
    byModel.get(key).push(s)
  }
  const result = []
  let idx = 0
  for (const [model, pts] of byModel) {
    pts.sort((a, b) => (a.bucket < b.bucket ? -1 : 1))
    const points = pts.map((p) => `${xOf(p.bucket).toFixed(1)},${yOf(p.value).toFixed(1)}`).join(' ')
    result.push({ model: model === '__all__' ? '' : model, points, color: PALETTE[idx % PALETTE.length] })
    idx += 1
  }
  return result
})

const gridLines = computed(() => {
  const d = props.data
  if (!d || !Array.isArray(d.series) || !d.series.length) return []
  const values = d.series.map((s) => s.value)
  const min = Math.min(...values)
  const max = Math.max(...values)
  const span = max - min || 1
  return [0, 0.25, 0.5, 0.75, 1].map((f) => ({
    y: H - PAD - f * (H - PAD * 2),
    label: (min + f * span).toFixed(max >= 100 ? 0 : 1),
  }))
})

const hasData = computed(() => lines.value.length > 0)
</script>

<template>
  <div class="chart" :style="{ height: height + 'px' }">
    <svg v-if="hasData" :viewBox="`0 0 ${W} ${H}`" preserveAspectRatio="none" class="svg">
      <g v-for="(g, i) in gridLines" :key="i">
        <line :x1="0" :y1="g.y" :x2="W" :y2="g.y" class="grid" />
        <text :x="2" :y="g.y - 3" class="ylab">{{ g.label }}</text>
      </g>
      <polyline
        v-for="(l, i) in lines"
        :key="i"
        :points="l.points"
        :stroke="l.color"
        fill="none"
        stroke-width="1.6"
        stroke-linejoin="round"
        stroke-linecap="round"
      />
    </svg>
    <div v-else class="empty">无数据</div>
    <div v-if="lines.length" class="legend">
      <span v-for="(l, i) in lines" :key="i" class="lg">
        <i class="dot" :style="{ background: l.color }" />
        {{ l.model || '全部' }}
      </span>
    </div>
  </div>
</template>

<style scoped>
.chart {
  position: relative;
  width: 100%;
}
.svg {
  width: 100%;
  height: 100%;
  display: block;
}
.grid {
  stroke: #eef0f3;
  stroke-width: 1;
}
.ylab {
  font-size: 9px;
  fill: #9aa1ab;
}
.empty {
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--text-2);
  font-size: 12px;
}
.legend {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  margin-top: 4px;
  font-size: 11px;
  color: var(--text-2);
}
.lg {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  display: inline-block;
}
</style>
