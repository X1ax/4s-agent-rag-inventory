"""Supplier quotes, retail pricing and immutable actual-cost snapshots."""
from alembic import op
import sqlalchemy as sa

revision = '0008_pricing'
down_revision = '0007_reviews'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('parts', sa.Column('sale_price', sa.Numeric(12, 2)))
    op.add_column('parts', sa.Column('sale_price_version', sa.Integer(), nullable=False, server_default='0'))
    op.create_check_constraint('ck_part_sale_price', 'parts', 'sale_price IS NULL OR sale_price >= 0')
    op.add_column('opening_lines', sa.Column('unit_cost', sa.Numeric(12, 2)))
    op.add_column('opening_lines', sa.Column('cost_source', sa.String(300), nullable=False, server_default=''))
    op.add_column('stock_balances', sa.Column('stock_value', sa.Numeric(24, 2)))
    op.add_column('stock_ledger', sa.Column('value_delta', sa.Numeric(24, 2)))
    op.add_column('documents', sa.Column('issue_type', sa.String(20)))
    for column, size in [('warranty_reason', 200), ('work_order', 80), ('pricing_source', 100)]:
        op.add_column('documents', sa.Column(column, sa.String(size), nullable=False, server_default=''))
    op.add_column('document_lines', sa.Column('sale_unit_price', sa.Numeric(12, 2)))
    op.add_column('document_lines', sa.Column('cost_amount', sa.Numeric(24, 2)))
    op.add_column('document_lines', sa.Column('quote_version', sa.Integer()))
    op.add_column('document_lines', sa.Column('sale_price_version', sa.Integer()))
    op.add_column('document_lines', sa.Column('pricing_source', sa.String(100), nullable=False, server_default=''))
    op.create_table('supplier_prices',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('supplier_id', sa.Integer(), sa.ForeignKey('suppliers.id'), nullable=False),
        sa.Column('part_id', sa.Integer(), sa.ForeignKey('parts.id'), nullable=False),
        sa.Column('supply_price', sa.Numeric(12, 2), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False), sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('source', sa.String(300), nullable=False), sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('supplier_id', 'part_id', name='uq_supplier_part_price'),
        sa.CheckConstraint('supply_price >= 0 AND version > 0', name='ck_supplier_price'))
    op.create_table('price_changes',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('part_id', sa.Integer(), sa.ForeignKey('parts.id'), nullable=False),
        sa.Column('supplier_id', sa.Integer(), sa.ForeignKey('suppliers.id')),
        sa.Column('kind', sa.String(20), nullable=False),
        sa.Column('before_price', sa.Numeric(12, 2)), sa.Column('after_price', sa.Numeric(12, 2), nullable=False),
        sa.Column('source', sa.String(300), nullable=False),
        sa.Column('operator_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False))
    op.create_table('pricing_backfills', sa.Column('version', sa.String(60), primary_key=True),
        sa.Column('plan_digest', sa.String(64), nullable=False), sa.Column('quantity_fingerprint', sa.String(64), nullable=False),
        sa.Column('summary', sa.JSON(), nullable=False), sa.Column('created_at', sa.DateTime(), nullable=False))


def downgrade():
    if any(op.get_bind().scalar(sa.text(query)) for query in (
        'SELECT COUNT(*) FROM supplier_prices', 'SELECT COUNT(*) FROM pricing_backfills',
        'SELECT COUNT(*) FROM price_changes', 'SELECT COUNT(*) FROM parts WHERE sale_price IS NOT NULL',
        'SELECT COUNT(*) FROM stock_balances WHERE stock_value IS NOT NULL',
        'SELECT COUNT(*) FROM stock_ledger WHERE value_delta IS NOT NULL',
        'SELECT COUNT(*) FROM opening_lines WHERE unit_cost IS NOT NULL',
        'SELECT COUNT(*) FROM document_lines WHERE sale_unit_price IS NOT NULL OR cost_amount IS NOT NULL')):
        raise RuntimeError('已有价格或成本数据，禁止回退丢失计价依据')
    op.drop_table('pricing_backfills')
    op.drop_table('price_changes')
    op.drop_table('supplier_prices')
    for table, columns in [('document_lines', ['sale_unit_price','cost_amount','quote_version','sale_price_version','pricing_source']),
        ('documents', ['issue_type','warranty_reason','work_order','pricing_source']), ('stock_ledger',['value_delta']),
        ('stock_balances',['stock_value']), ('opening_lines',['unit_cost','cost_source'])]:
        for column in columns:
            op.drop_column(table, column)
    op.drop_constraint('ck_part_sale_price', 'parts', type_='check')
    op.drop_column('parts', 'sale_price')
    op.drop_column('parts', 'sale_price_version')
