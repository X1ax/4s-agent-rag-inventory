export function proposalRows(proposal) {
  return (proposal.payload.lines || proposal.summary.lines).map(line => {
    const original = (line.purchase_line_id == null ? line : proposal.summary.lines.find(row => row.line_id === line.purchase_line_id)) || line
    const part = proposal.summary.parts.find(part => part.id === original.part_id)
    const supplier = proposal.summary.suppliers.find(supplier => supplier.id === (line.supplier_id || original.supplier_id || proposal.payload.supplier_id))
    return { ...line, sku: part?.sku, name: part?.name, supplier: supplier?.name, unit_price: line.unit_price || original.unit_price }
  })
}
