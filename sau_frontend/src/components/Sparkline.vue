<template>
  <div class="sparkline" :class="{ 'is-empty': !hasData }">
    <svg
      v-if="hasData"
      :viewBox="`0 0 ${width} ${height}`"
      preserveAspectRatio="none"
      class="sparkline-svg"
      @mouseleave="hoverIndex = null"
    >
      <defs>
        <linearGradient :id="gradientId" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" :stop-color="color" stop-opacity="0.28" />
          <stop offset="100%" :stop-color="color" stop-opacity="0.02" />
        </linearGradient>
      </defs>

      <!-- 零轴：涨粉数据有正有负，没有这条线的话"贴着底边"分不清是 0 还是负 -->
      <line
        v-if="hasNegative"
        :x1="0"
        :x2="width"
        :y1="zeroY"
        :y2="zeroY"
        class="sparkline-zero"
      />

      <path v-if="areaPath" :d="areaPath" :fill="`url(#${gradientId})`" stroke="none" />
      <path v-if="linePath" :d="linePath" fill="none" :stroke="color" stroke-width="1.8"
            stroke-linejoin="round" stroke-linecap="round" />
      <circle
        v-if="hoverPoint"
        :cx="hoverPoint.x"
        :cy="hoverPoint.y"
        r="3.2"
        :fill="color"
        stroke="#fff"
        stroke-width="1.4"
      />
    </svg>
    <span v-else class="sparkline-empty">暂无趋势数据</span>

    <!-- 悬浮层：每个点一个热区，比在 svg 上算坐标简单也更稳 -->
    <div v-if="hasData" class="sparkline-hit">
      <div
        v-for="(point, index) in points"
        :key="point.date"
        class="sparkline-hit-cell"
        @mouseenter="hoverIndex = index"
      />
    </div>

    <div v-if="hoverPoint" class="sparkline-tip" :style="tipStyle">
      <span class="tip-date">{{ hoverPoint.date }}</span>
      <span class="tip-value">{{ formatValue(hoverPoint.value) }}</span>
    </div>

    <div v-if="hasData" class="sparkline-axis">
      <span>{{ points[0].date }}</span>
      <span v-if="hoverPoint" class="axis-hover">{{ hoverPoint.date }}</span>
      <span>{{ points[points.length - 1].date }}</span>
    </div>
  </div>
</template>

<script setup>
/**
 * 迷你趋势图（at-ai-xhs 改造新增）。
 *
 * **为什么手写 SVG 而不引图表库**：这个项目前端只依赖 vue / element-plus / axios，
 * 为了一张 20 来个点的小折线去装 echarts（几 MB、还要单独分包）不成比例。
 * 这里要的东西其实很少：一条线 + 一块渐变 + 悬浮看单点。
 *
 * 收到的数据是**平台侧的真实序列**，所以有两件事必须在图里体现，否则会误导人：
 *  1. **0 值是真的**：涨粉数据大量是 0，全 0 时不能画成"一条贴在中间的线"，
 *     而应该贴底——所以按 `max(最大值, 0)` 归一化，负值才单独留出空间；
 *  2. **负值**（掉粉）要能看出来：有负数时画一条零轴，并在下方留高度。
 */
import { computed, ref } from 'vue'

const props = defineProps({
  /** `[{date: '2026-09-30', count: 3}, ...]`，按日期升序 */
  data: { type: Array, default: () => [] },
  color: { type: String, default: '#409eff' },
  height: { type: Number, default: 64 },
  /** 数值的单位后缀，例如「个」「次」。只影响悬浮提示的文案 */
  unit: { type: String, default: '' }
})

const width = 300

// 每个实例一个独立的渐变 id：同一个页面里可能同时有好几张图，
// 共用 `id="grad"` 会让后来的那个把前面那张的填充色顶掉。
const gradientId = `sparkline-grad-${Math.random().toString(36).slice(2, 9)}`

const hoverIndex = ref(null)

const points = computed(() =>
  (props.data || [])
    .filter((row) => row && row.date && row.count !== null && row.count !== undefined)
    .map((row) => ({ date: String(row.date), value: Number(row.count) || 0 }))
)

const hasData = computed(() => points.value.length >= 2)

const bounds = computed(() => {
  const values = points.value.map((p) => p.value)
  const max = Math.max(...values, 0)
  const min = Math.min(...values, 0)
  // 全 0 的时候给个 1，否则除数为 0
  const span = max - min || 1
  return { max, min, span }
})

const hasNegative = computed(() => bounds.value.min < 0)

/** 数值 → y 坐标。上边留 4px、下边留 10px 给悬浮圆点和零轴。 */
function toY(value) {
  const { max, span } = bounds.value
  const usable = props.height - 14
  return 4 + ((max - value) / span) * usable
}

function toX(index) {
  if (points.value.length <= 1) return width / 2
  return (index / (points.value.length - 1)) * width
}

const linePath = computed(() => {
  if (!hasData.value) return ''
  return points.value
    .map((point, index) => `${index === 0 ? 'M' : 'L'}${toX(index).toFixed(2)},${toY(point.value).toFixed(2)}`)
    .join(' ')
})

const areaPath = computed(() => {
  if (!linePath.value) return ''
  const first = toX(0).toFixed(2)
  const last = toX(points.value.length - 1).toFixed(2)
  const baseY = (hasNegative.value ? toY(0) : props.height).toFixed(2)
  return `${linePath.value} L${last},${baseY} L${first},${baseY} Z`
})

const zeroY = computed(() => toY(0))

const hoverPoint = computed(() => {
  if (hoverIndex.value === null) return null
  const point = points.value[hoverIndex.value]
  if (!point) return null
  return { ...point, x: toX(hoverIndex.value), y: toY(point.value) }
})

const tipStyle = computed(() => {
  if (!hoverPoint.value) return {}
  // 悬浮提示跟着点在横向移动，并夹在左右边界内，避免被容器裁掉
  const ratio = hoverPoint.value.x / width
  return {
    left: `${Math.min(88, Math.max(12, ratio * 100))}%`
  }
})

function formatValue(value) {
  const text = Number.isInteger(value) ? String(value) : value.toFixed(2)
  return `${value > 0 && props.unit === '个' ? '+' : ''}${text}${props.unit}`
}
</script>

<style lang="scss" scoped>
.sparkline {
  position: relative;
  width: 100%;

  &.is-empty {
    display: flex;
    align-items: center;
    justify-content: center;
  }
}

.sparkline-svg {
  display: block;
  width: 100%;
  height: 64px;
  overflow: visible;
}

.sparkline-zero {
  stroke: #dcdfe6;
  stroke-width: 1;
  stroke-dasharray: 3 3;
}

.sparkline-empty {
  font-size: 12px;
  color: #c0c4cc;
  line-height: 64px;
}

.sparkline-hit {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: 64px;
  display: flex;
}

.sparkline-hit-cell {
  flex: 1;
  height: 100%;
}

.sparkline-tip {
  position: absolute;
  top: -6px;
  transform: translate(-50%, -100%);
  background: rgba(48, 49, 51, 0.92);
  color: #fff;
  font-size: 12px;
  line-height: 1.5;
  padding: 4px 8px;
  border-radius: 4px;
  white-space: nowrap;
  pointer-events: none;
  display: flex;
  gap: 8px;
  z-index: 2;
}

.tip-date {
  opacity: 0.75;
}

.tip-value {
  font-weight: 600;
}

.sparkline-axis {
  display: flex;
  justify-content: space-between;
  font-size: 11px;
  color: #a8abb2;
  margin-top: 2px;
  min-height: 16px;
}

.axis-hover {
  color: #606266;
  font-weight: 600;
}
</style>
