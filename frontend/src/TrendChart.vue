<script setup>
import { computed, onMounted, onBeforeUnmount, ref, watch } from 'vue'
import { chartScale, chartValue, nearestDateIndex, smoothPath, stackSeries } from './trendChart.js'

const props = defineProps({ dates: { type: Array, required: true }, series: { type: Array, required: true }, unit: { type: String, default: '件' }, label: String, stacked: Boolean, stretch: Boolean })
const host = ref(null), plot = ref(null), width = ref(720), height = ref(288), active = ref(-1), hidden = ref([])
const visible = computed(() => props.series.filter(series => !hidden.value.includes(series.key)))
const stack = computed(() => stackSeries(visible.value, props.dates.length))
const scale = computed(() => chartScale(props.stacked ? stack.value.totals : visible.value.flatMap(series => series.values.map(Number)), props.unit === '件'))
const left = 60, top = 22
const bottom = computed(() => height.value - 38)
const right = computed(() => width.value - 18)
const x = index => props.stacked ? left + (index + .5) / Math.max(1, props.dates.length) * (right.value - left)
  : props.dates.length === 1 ? (left + right.value) / 2 : left + index / Math.max(1, props.dates.length - 1) * (right.value - left)
const barWidth = computed(() => Math.min(26, (right.value - left) / Math.max(1, props.dates.length) * .7))
const y = value => bottom.value - (Number(value) - scale.value.low) / (scale.value.high - scale.value.low) * (bottom.value - top)
const curves = computed(() => visible.value.map(series => ({ ...series, path: smoothPath(series.values.map((value, index) => [x(index), y(value)])) })))
const dateTicks = computed(() => {
  const count = width.value < 440 ? 3 : 4
  return [...new Set(Array.from({ length: count }, (_, i) => Math.round(i / (count - 1) * (props.dates.length - 1))))].filter(index => index >= 0 && index < props.dates.length)
})
const tooltipLeft = computed(() => Math.max(0, Math.min(width.value - Math.min(270, width.value), x(active.value) + (x(active.value) > width.value / 2 ? -284 : 14))))
const axisLabel = value => value.toLocaleString('zh-CN', { maximumFractionDigits: 2, ...(Math.abs(value) >= 10000 ? { notation: 'compact' } : {}) })
function toggle(key) {
  hidden.value = hidden.value.includes(key) ? hidden.value.filter(item => item !== key) : [...hidden.value, key]
  active.value = -1
}
function point(event) {
  if (!visible.value.length) return
  const rect = event.currentTarget.getBoundingClientRect()
  active.value = nearestDateIndex((event.clientX - rect.left) / rect.width * width.value, left, right.value, props.dates.length, props.stacked)
}
function key(event) {
  if (!visible.value.length || !props.dates.length) return
  if (event.key === 'Escape') { active.value = -1; return }
  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
  event.preventDefault()
  active.value = event.key === 'Home' ? 0 : event.key === 'End' ? props.dates.length - 1
    : Math.max(0, Math.min(props.dates.length - 1, active.value + (event.key === 'ArrowRight' ? 1 : -1)))
}
let observer
onMounted(() => {
  observer = new ResizeObserver(entries => {
    for (const entry of entries) {
      if (entry.target === host.value) width.value = Math.max(260, entry.contentRect.width)
      else if (props.stretch) height.value = Math.max(288, entry.contentRect.height)
    }
  })
  observer.observe(host.value)
  observer.observe(plot.value)
})
onBeforeUnmount(() => observer?.disconnect())
watch(() => props.dates, () => { active.value = -1 })
</script>

<template>
  <div class="interactive-trend" :class="{ stretch }" ref="host">
    <div class="trend-controls"><div class="trend-legends" aria-label="切换显示的数据系列"><button v-for="series in props.series" :key="series.key" type="button" :aria-pressed="!hidden.includes(series.key)" :class="{ inactive: hidden.includes(series.key) }" @click="toggle(series.key)"><i :style="{ background: series.color }" />{{ series.label }}</button></div><span class="trend-unit">单位：{{ unit }}</span></div>
    <div class="trend-plot" ref="plot">
      <svg :viewBox="`0 0 ${width} ${height}`" :style="{ height: stretch ? '100%' : `${height}px` }" tabindex="0" role="img" :aria-label="`${label}，单位${unit}。左右方向键查看每日数据，Home 和 End 查看首尾日期，Escape 关闭提示。`" @pointermove="point" @pointerdown="point" @pointerleave="active = -1" @keydown="key" @blur="active = -1">
        <g v-for="tick in scale.ticks" :key="tick"><line :x1="left" :x2="right" :y1="y(tick)" :y2="y(tick)" :class="{ 'zero-line': tick === 0 }" class="trend-grid" /><text :x="left - 12" :y="y(tick) + 4" text-anchor="end">{{ axisLabel(tick) }}</text></g>
        <template v-if="stacked"><g v-for="series in stack.layers" :key="series.key"><rect v-for="(point, index) in series.points" :key="index" :x="x(index) - barWidth / 2" :y="y(point.end)" :width="barWidth" :height="Math.max(0, y(point.start) - y(point.end))" :fill="series.color" :opacity="active < 0 || active === index ? 1 : .4" /></g></template>
        <template v-else><g v-for="series in curves" :key="series.key">
          <path v-if="dates.length > 1" :d="`${series.path} L ${x(dates.length - 1)},${y(0)} L ${x(0)},${y(0)} Z`" :fill="series.color" fill-opacity=".09" />
          <path :d="series.path" fill="none" :stroke="series.color" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" />
          <circle v-if="dates.length === 1" :cx="x(0)" :cy="y(series.values[0])" r="4" :fill="series.color" />
        </g></template>
        <text v-for="index in dateTicks" :key="index" :x="x(index)" :y="height - 10" :text-anchor="dates.length === 1 ? 'middle' : index === 0 ? 'start' : index === dates.length - 1 ? 'end' : 'middle'">{{ dates[index]?.slice(5).replace('-', '/') }}</text>
        <g v-if="active >= 0 && visible.length"><line :x1="x(active)" :x2="x(active)" :y1="top" :y2="bottom" class="trend-guide" /><template v-if="!stacked"><circle v-for="series in visible" :key="series.key" :cx="x(active)" :cy="y(series.values[active])" r="4" fill="white" :stroke="series.color" stroke-width="2" /></template></g>
      </svg>
      <div v-if="active >= 0 && visible.length" class="trend-tooltip" role="status" :style="{ left: `${tooltipLeft}px` }"><strong>{{ dates[active] }}</strong><div v-for="series in visible" :key="series.key"><span><i :style="{ background: series.color }" />{{ series.label }}</span><b>{{ chartValue(series.values[active], unit) }} <small>{{ unit }}</small></b></div><div v-if="stacked" class="tooltip-total"><span>{{ hidden.length ? '所选合计' : '合计' }}</span><b>{{ chartValue(stack.totals[active], unit) }} <small>{{ unit }}</small></b></div></div>
      <p v-if="!visible.length" class="trend-overlay">点击上方图例显示数据</p><p v-else-if="!dates.length" class="trend-overlay">当前范围暂无趋势数据</p>
    </div>
  </div>
</template>

<style scoped>
.interactive-trend { min-width: 0; margin-top: 18px; }
.interactive-trend.stretch { flex: 1; display: flex; flex-direction: column; }
.stretch .trend-plot { flex: 1; min-height: 288px; }
.stretch svg { position: absolute; inset: 0; }
.trend-controls, .trend-legends { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.trend-controls { justify-content: space-between; margin-bottom: 12px; }
.trend-legends button { border: 0; background: transparent; color: #53617c; display: inline-flex; align-items: center; gap: 7px; padding: 6px 9px; border-radius: 8px; font: inherit; font-size: 12px; cursor: pointer; transition: background .15s, color .15s; }
.trend-legends button:hover, .trend-legends button:focus-visible { background: #e6edfa; color: #146cff; }
.trend-legends button.inactive { opacity: .45; text-decoration: line-through; }
.trend-legends i, .trend-tooltip i { width: 9px; height: 9px; border-radius: 3px; flex: none; }
.trend-unit { color: #778199; font-size: 11px; }
.trend-plot { position: relative; }
svg { display: block; width: 100%; overflow: visible; touch-action: pan-y; border-radius: 8px; outline: none; }
svg:focus-visible { outline: 2px solid #9cbfff; outline-offset: 4px; }
svg text { font-family: inherit; font-size: 12px; fill: #73809a; }
.trend-grid { stroke: #dce2ee; stroke-width: 1; }
.trend-grid.zero-line { stroke: #c7d1e3; }
.trend-guide { stroke: #a5b7d3; stroke-dasharray: 4 4; }
.trend-tooltip { position: absolute; top: 34px; box-sizing: border-box; width: 270px; max-width: 100%; padding: 18px 20px; border-radius: 20px; background: #fff; box-shadow: 0 10px 32px #202d451a, 0 1px 2px #202d4510; color: #242a35; pointer-events: none; z-index: 2; font-size: 13px; }
.trend-tooltip > strong { display: block; margin-bottom: 12px; font-size: 14px; font-variant-numeric: tabular-nums; }
.trend-tooltip > div { display: flex; justify-content: space-between; gap: 12px; margin-top: 9px; }
.trend-tooltip span { display: flex; align-items: center; gap: 8px; color: #737e92; }
.trend-tooltip b { font-weight: 500; font-variant-numeric: tabular-nums; white-space: nowrap; }
.trend-tooltip small { font-size: 10px; color: #7d8799; }
.trend-tooltip .tooltip-total { border-top: 1px solid #e7ecf3; padding-top: 10px; }
.trend-overlay { position: absolute; top: 40%; left: 0; width: 100%; text-align: center; color: #748198; font-size: 13px; pointer-events: none; }
</style>
