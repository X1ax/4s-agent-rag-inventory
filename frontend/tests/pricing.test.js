import test from 'node:test'
import assert from 'node:assert/strict'
import { activeQuote, documentLines, catalogPayload, moneyText } from '../src/pricing.js'

test('a missing or disabled quote cannot become a zero price; an explicit zero quote remains usable', () => {
  const part = { suppliers: [{ id: 1, supply_price: null, active: true, version: 0 }, { id: 2, supply_price: '0.00', active: true, version: 1 }, { id: 3, supply_price: '5.00', active: false, version: 2 }] }
  assert.equal(activeQuote(part, 1), null)
  assert.equal(activeQuote(part, 2)?.supply_price, '0.00')
  assert.equal(activeQuote(part, 3), null)
  assert.equal(activeQuote(undefined, 2), null)
})

test('document payloads retain a purchase quote version but discard client settlement and cost fields', () => {
  const lines = [{ part_id: 4, supplier_id: 2, quantity: 3, unit_price: '8.10', quote_version: 7, sale_unit_price: '99.00', cost_amount: '1.00', sale_price_version: 8 }]
  assert.deepEqual(documentLines('purchase', lines), [{ part_id: 4, supplier_id: 2, quantity: 3, unit_price: '8.10', quote_version: 7 }])
  assert.deepEqual(documentLines('issue', lines), [{ part_id: 4, quantity: 3 }])
})

test('ordinary catalog updates cannot overwrite independently managed prices or read-only metadata', () => {
  assert.deepEqual(catalogPayload({ id: 1, name: '备件', reviewed_by: 2, reviewed_at: 'today', sale_price: '50.00', sale_price_version: 2, suppliers: [], default_supply_price: '25.00' }), { name: '备件' })
})

test('profit amounts preserve negative cents and show unavailable rather than zero for missing values', () => {
  assert.equal(moneyText('-0.05'), '-0.05')
  assert.equal(moneyText('12345678901234567890.20'), '12345678901234567890.20')
  assert.equal(moneyText(null), '—')
  assert.equal(moneyText('0.00'), '0.00')
})
