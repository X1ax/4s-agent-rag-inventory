// Decimal prices stay strings; use integer cents even for large purchase totals.
export function purchaseLineCents(price, quantity) {
  const text = String(price ?? '').trim()
  if (!/^\d{1,10}(?:\.\d{1,2})?$/.test(text) || !Number.isInteger(quantity) || quantity < 1 || quantity > 1_000_000_000) return null
  const [whole, fraction = ''] = text.split('.')
  return (BigInt(whole) * 100n + BigInt(fraction.padEnd(2, '0'))) * BigInt(quantity)
}

export function formatPurchaseAmount(cents) {
  if (cents === null) return '—'
  return `${cents / 100n}.${String(cents % 100n).padStart(2, '0')}`
}
