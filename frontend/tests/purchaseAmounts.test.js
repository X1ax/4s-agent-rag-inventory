import test from 'node:test'
import assert from 'node:assert/strict'
import { purchaseLineCents, formatPurchaseAmount } from '../src/purchaseAmounts.js'

test('decimal prices and quantities produce exact cents, including zero prices', () => {
  assert.equal(purchaseLineCents('0.10', 3), 30n)
  assert.equal(formatPurchaseAmount(purchaseLineCents('12.35', 7)), '86.45')
  assert.equal(formatPurchaseAmount(purchaseLineCents('0.00', 2)), '0.00')
  assert.equal(formatPurchaseAmount(purchaseLineCents(' 35.5 ', 2)), '71.00')
})

test('invalid or unfinished prices and quantities do not display a misleading total', () => {
  for (const price of ['', '1.', '-1', '1.234', 'NaN', '1e2', '10000000000.00']) assert.equal(purchaseLineCents(price, 1), null)
  for (const quantity of [null, 0, -1, 1.5, NaN, 1_000_000_001]) assert.equal(purchaseLineCents('1.00', quantity), null)
  assert.equal(formatPurchaseAmount(null), '—')
})

test('multi-line and maximum-size totals retain precision above Number safe range', () => {
  const total = purchaseLineCents('0.10', 3) + purchaseLineCents('12.35', 7)
  assert.equal(formatPurchaseAmount(total), '86.75')
  assert.equal(purchaseLineCents('9999999999.99', 1_000_000_000), 999999999999000000000n)
  assert.equal(formatPurchaseAmount(purchaseLineCents('9999999999.99', 1_000_000_000)), '9999999999990000000.00')
})
