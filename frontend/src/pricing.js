import { purchaseLineCents } from './purchaseAmounts.js'

export function activeQuote(part, supplierId) {
  return part?.suppliers?.find(quote => quote.id === supplierId && quote.active && quote.version > 0 && purchaseLineCents(quote.supply_price, 1) !== null) || null
}

export function documentLines(kind, lines) {
  return lines.map(line => ({ part_id: line.part_id, quantity: line.quantity,
    ...(kind === 'purchase' ? { supplier_id: line.supplier_id, unit_price: line.unit_price, quote_version: line.quote_version } : {}) }))
}

export function catalogPayload(value) {
  const result = { ...value }
  for (const key of ['id', 'reviewed_by', 'reviewed_at', 'sale_price', 'sale_price_version', 'suppliers', 'default_supply_price']) delete result[key]
  return result
}

export function moneyText(value) {
  if (value === null || value === undefined || value === '') return '—'
  const text = String(value)
  if (!/^-?\d+(?:\.\d{1,2})?$/.test(text)) return '—'
  const [whole, fraction = ''] = text.split('.')
  return `${whole}.${fraction.padEnd(2, '0')}`
}
