import test from 'node:test'
import assert from 'node:assert/strict'
import { proposalRows } from '../src/proposalRows.js'

const summary = {
  lines: [{ part_id: 3, quantity: 1 }, { part_id: 5, quantity: 4 }],
  parts: [{ id: 3, sku: 'DEMO-003', name: '空调滤清器' }, { id: 5, sku: 'DEMO-005', name: '制动液' }],
  suppliers: [{ id: 1, name: '保养件' }, { id: 2, name: '油液' }],
}
test('new purchase proposal keeps each line identity, supplier, quantity and quote', () => {
  const rows = proposalRows({ summary, payload: { lines: [
    { part_id: 3, supplier_id: 1, quantity: 1, unit_price: '59.00' },
    { part_id: 5, supplier_id: 2, quantity: 4, unit_price: '85.00' },
  ] } })
  assert.deepEqual(rows.map(row => [row.sku, row.name, row.quantity, row.supplier, row.unit_price]), [
    ['DEMO-003', '空调滤清器', 1, '保养件', '59.00'], ['DEMO-005', '制动液', 4, '油液', '85.00'],
  ])
})
test('receipt identifies the original purchase line by explicit line ID rather than array order', () => {
  const rows = proposalRows({ summary: { ...summary, lines: [
    { line_id: 10, part_id: 3, supplier_id: 1, unit_price: '59.00' },
    { line_id: 20, part_id: 5, supplier_id: 2, unit_price: '85.00' },
  ] }, payload: { lines: [{ purchase_line_id: 20, quantity: 2 }] } })
  assert.equal(rows[0].sku, 'DEMO-005')
  assert.equal(rows[0].supplier, '油液')
  assert.equal(rows[0].quantity, 2)
})
