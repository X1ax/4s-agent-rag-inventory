export const stockPosition = row => row.quantity + row.in_transit + row.purchase_pending

export function filterAttention(rows, onlyUncovered) {
  return onlyUncovered ? rows.filter(row => stockPosition(row) < row.safety_stock) : rows
}
