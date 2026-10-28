import test from 'node:test'
import assert from 'node:assert/strict'
import { stockPosition, filterAttention } from '../src/stockAttention.js'

test('pending stock is evaluated for each warehouse-part row, without merging another warehouse', () => {
  const rows = [
    { warehouse_id: 1, part_id: 3, quantity: 2, safety_stock: 10, in_transit: 3, purchase_pending: 5 },
    { warehouse_id: 2, part_id: 3, quantity: 2, safety_stock: 10, in_transit: 1, purchase_pending: 2 },
    { warehouse_id: 2, part_id: 4, quantity: 0, safety_stock: 1, in_transit: 0, purchase_pending: 0 },
    { warehouse_id: 1, part_id: 5, quantity: 1, safety_stock: 3, in_transit: 0, purchase_pending: 5 },
  ]
  assert.equal(stockPosition(rows[0]), 10)
  assert.deepEqual(filterAttention(rows, true), [rows[1], rows[2]])
  assert.deepEqual(filterAttention(rows, false), rows)
  assert.deepEqual(filterAttention([], true), [])
  assert.equal(rows[0].quantity, 2)
})
