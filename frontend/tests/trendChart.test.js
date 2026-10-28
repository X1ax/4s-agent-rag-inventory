import test from 'node:test'
import assert from 'node:assert/strict'
import { dateRange, chartScale, smoothPath, nearestDateIndex, chartValue, stackSeries } from '../src/trendChart.js'

test('daily axis crosses month boundaries without omitting zero-record dates', () => {
  assert.deepEqual(dateRange('2026-09-29', '2026-10-02'), ['2026-09-29', '2026-09-30', '2026-10-01', '2026-10-02'])
  assert.deepEqual(dateRange('2026-10-02', '2026-10-02'), ['2026-10-02'])
  assert.deepEqual(dateRange('2026-10-03', '2026-10-02'), [])
})

test('axis includes zero and negative profit, with integer ticks for quantities and a nonzero empty range', () => {
  for (const values of [[], [0, 0], [1], [-1123, 7230], [-0.05, 0.12]]) {
    const scale = chartScale(values, false)
    assert.ok(scale.high > scale.low)
    assert.ok(scale.low <= Math.min(0, ...values) && scale.high >= Math.max(0, ...values))
    assert.ok(scale.ticks.includes(0))
  }
  assert.ok(chartScale([1, 2], true).ticks.every(Number.isInteger))
})

test('smooth curve passes through samples and stays within each segment range, including zero plateaus', () => {
  const points = [0, 0, 173, 0, 0, -20, 45, 45].map((y, i) => [i * 20, y])
  const path = smoothPath(points)
  const segments = [...path.matchAll(/C ([\d.e+-]+),([\d.e+-]+) ([\d.e+-]+),([\d.e+-]+) ([\d.e+-]+),([\d.e+-]+)/g)]
  assert.equal(segments.length, points.length - 1)
  segments.forEach((segment, index) => {
    const [, , y1, , y2, x3, y3] = segment.map(Number)
    assert.equal(x3, points[index + 1][0])
    assert.equal(y3, points[index + 1][1])
    const start = points[index][1], end = points[index + 1][1]
    for (let step = 0; step <= 100; step++) {
      const t = step / 100
      const y = (1-t)**3*start + 3*(1-t)**2*t*y1 + 3*(1-t)*t*t*y2 + t**3*end
      assert.ok(y >= Math.min(start, end) - 1e-8 && y <= Math.max(start, end) + 1e-8)
    }
  })
  assert.equal(smoothPath([]), '')
  assert.equal(smoothPath([[100, 20]]), 'M 100,20')
})

test('pointer selects nearest real date and clamps to chart edges, including single day', () => {
  assert.equal(nearestDateIndex(-50, 50, 350, 4), 0)
  assert.equal(nearestDateIndex(205, 50, 350, 4), 2)
  assert.equal(nearestDateIndex(900, 50, 350, 4), 3)
  assert.equal(nearestDateIndex(200, 50, 350, 1), 0)
  assert.equal(nearestDateIndex(200, 50, 350, 0), -1)
})

test('tooltip preserves exact monetary cents and distinguishes missing values from recorded zero', () => {
  assert.equal(chartValue('-0.05', '元'), '-0.05')
  assert.equal(chartValue('12345678901234567890.20', '元'), '12,345,678,901,234,567,890.20')
  assert.equal(chartValue(null, '元'), '—')
  assert.equal(chartValue(0, '元'), '0.00')
  assert.equal(chartValue(144, '件'), '144')
})

test('daily paid and warranty bars stack quantities without mixing money or changing source arrays', () => {
  const paid = { key: 'paid', values: [10, 0, 3] }, warranty = { key: 'warranty', values: [2, 0, 5] }
  const result = stackSeries([paid, warranty], 3)
  assert.deepEqual(result.totals, [12, 0, 8])
  assert.deepEqual(result.layers[1].points, [{ start: 10, end: 12, value: 2 }, { start: 0, end: 0, value: 0 }, { start: 3, end: 8, value: 5 }])
  assert.deepEqual(stackSeries([warranty], 3).totals, [2, 0, 5])
  assert.deepEqual(paid.values, [10, 0, 3])
  assert.deepEqual(stackSeries([], 3).totals, [0, 0, 0])
  assert.equal(nearestDateIndex(100, 50, 350, 3, true), 0)
  assert.equal(nearestDateIndex(200, 50, 350, 3, true), 1)
  assert.equal(nearestDateIndex(300, 50, 350, 3, true), 2)
})
