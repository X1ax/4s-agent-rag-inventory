"""Supplier coverage and supplier attribution on each purchase/receipt line."""
from alembic import op
import sqlalchemy as sa

revision = "0005_suppliers"
down_revision = "0004_catalog"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("suppliers", sa.Column("supply_categories", sa.JSON(), nullable=True))
    op.execute("UPDATE suppliers SET supply_categories = JSON_ARRAY()")
    op.alter_column("suppliers", "supply_categories", existing_type=sa.JSON(), nullable=False)
    op.add_column("document_lines", sa.Column("supplier_id", sa.Integer(), nullable=True))
    op.create_foreign_key("fk_document_line_supplier", "document_lines", "suppliers", ["supplier_id"], ["id"])
    op.execute("UPDATE document_lines l JOIN documents d ON d.id = l.document_id SET l.supplier_id = d.supplier_id WHERE d.kind IN ('purchase','receipt')")
    op.drop_constraint("ck_purchase_supplier", "documents", type_="check")


def downgrade():
    connection = op.get_bind()
    if (connection.scalar(sa.text("SELECT COUNT(*) FROM suppliers WHERE JSON_LENGTH(supply_categories) > 0"))
            or connection.scalar(sa.text("SELECT COUNT(*) FROM documents WHERE kind = 'purchase' AND supplier_id IS NULL"))
            or connection.scalar(sa.text("SELECT COUNT(*) FROM document_lines l JOIN documents d ON d.id = l.document_id WHERE NOT (l.supplier_id <=> d.supplier_id) AND d.kind IN ('purchase','receipt')"))):
        raise RuntimeError("已有供货范围或逐行供应商资料，禁止回退丢失这些数据")
    op.create_check_constraint("ck_purchase_supplier", "documents", "kind != 'purchase' OR supplier_id IS NOT NULL")
    op.drop_constraint("fk_document_line_supplier", "document_lines", type_="foreignkey")
    op.drop_column("document_lines", "supplier_id")
    op.drop_column("suppliers", "supply_categories")
