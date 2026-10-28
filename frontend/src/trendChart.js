import { moneyText } from './pricing.js'

export function dateRange(from, to) {
  const dates = []
  for (let time = Date.parse(`${from}T00:00:00Z`); time <= Date.parse(`${to}T00:00:00Z`); time += 86400000) dates.push(new Date(time).toISOString().slice(0, 10))
  return dates
}

export function chartScale(values, integer = true) {
  const minimum = Math.min(0, ...values), maximum = Math.max(0, ...values)
  const rough = (maximum - minimum || 1) / 3
  const power = 10 ** Math.floor(Math.log10(rough))
  const niceStep = [1, 2, 2.5, 5, 10].find(value => value * power >= rough) * power
  const step = integer ? Math.max(1, Math.ceil(niceStep)) : niceStep
  const low = Math.floor(minimum / step) * step, high = Math.max(step, Math.ceil(maximum / step) * step)
  const ticks = Array.from({ length: Math.round((high - low) / step) + 1 }, (_, index) => Number((low + index * step).toPrecision(12)))
  return { low, high, ticks }
}

// Shape-preserving Hermite interpolation: no averaged data or invented extrema.
export function smoothPath(points) {
  const slopes = points.slice(1).map((point, index) => (point[1] - points[index][1]) / (point[0] - points[index][0]))
  const tangents = points.map((_, index) => index === 0 ? slopes[0] || 0 : index === points.length - 1 ? slopes.at(-1) || 0
    : slopes[index - 1] * slopes[index] <= 0 ? 0 : 2 * slopes[index - 1] * slopes[index] / (slopes[index - 1] + slopes[index]))
  return points.map((point, index) => {
    if (!index) return `M ${point[0]},${point[1]}`
    const previous = points[index - 1], third = (point[0] - previous[0]) / 3
    return `C ${previous[0] + third},${previous[1] + tangents[index - 1] * third} ${point[0] - third},${point[1] - tangents[index] * third} ${point[0]},${point[1]}`
  }).join(' ')
}

export function nearestDateIndex(x, left, right, count, stacked = false) {
  return count ? Math.max(0, Math.min(count - 1, Math.round((x - left) / (right - left) * (stacked ? count : count - 1) - (stacked ? .5 : 0)))) : -1
}

export function stackSeries(series, count) {
  const totals = Array(count).fill(0)
  const layers = series.map(item => ({ ...item, points: item.values.map((value, index) => {
    const start = totals[index]
    totals[index] += Number(value)
    return { start, end: totals[index], value: Number(value) }
  }) }))
  return { totals, layers }
}

export function chartValue(value, unit) {
  if (value == null) return '—'
  const text = unit === '元' ? moneyText(value) : String(value)
  const [whole, fraction] = text.split('.')
  return whole.replace(/\B(?=(\d{3})+(?!\d))/g, ',') + (fraction === undefined ? '' : `.${fraction}`)
}
