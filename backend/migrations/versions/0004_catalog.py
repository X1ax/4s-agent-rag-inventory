"""Vehicle conditions, traceable part identity and fitment evidence.

Revision ID: 0004_catalog
Revises: 0003_m2
"""
from alembic import op
import sqlalchemy as sa

revision = "0004_catalog"
down_revision = "0003_m2"
branch_labels = None
depends_on = None


def review_columns():
    return [sa.Column("reviewed_by", sa.Integer(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["reviewed_by"], ["users.id"])]


def upgrade():
    for name, length in [("aliases", 200), ("manufacturer", 100), ("manufacturer_part_number", 100),
        ("specification", 200), ("source_reference", 300), ("source_version", 60)]:
        op.add_column("parts", sa.Column(name, sa.String(length), nullable=False, server_default=""))
    op.add_column("parts", sa.Column("reviewed_by", sa.Integer(), nullable=True))
    op.add_column("parts", sa.Column("reviewed_at", sa.DateTime(), nullable=True))
    op.create_foreign_key("fk_part_reviewer", "parts", "users", ["reviewed_by"], ["id"])
    op.create_table("vehicle_models",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(40), nullable=False, unique=True),
        sa.Column("model_year", sa.Integer(), nullable=True),
        *[sa.Column(name, sa.String(length), nullable=False, server_default="") for name, length in [
            ("brand", 80), ("model_name", 100), ("powertrain_type", 60), ("engine_code", 100),
            ("transmission", 100), ("market", 80), ("configuration", 300),
            ("source_reference", 300), ("source_version", 60)]],
        sa.Column("active", sa.Boolean(), nullable=False),
        *review_columns(),
        sa.CheckConstraint("model_year IS NULL OR model_year BETWEEN 1900 AND 2100", name="ck_vehicle_year"))
    op.create_table("part_fitments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("vehicle_id", sa.Integer(), sa.ForeignKey("vehicle_models.id"), nullable=False),
        sa.Column("part_id", sa.Integer(), sa.ForeignKey("parts.id"), nullable=False),
        sa.Column("required_quantity", sa.Integer(), nullable=True),
        *[sa.Column(name, sa.String(length), nullable=False, server_default="") for name, length in [
            ("conditions", 500), ("restrictions", 500), ("source_reference", 300), ("source_version", 60)]],
        sa.Column("active", sa.Boolean(), nullable=False),
        *review_columns(),
        sa.CheckConstraint("required_quantity IS NULL OR required_quantity > 0", name="ck_fitment_quantity"))
    op.create_index("ix_part_fitments_vehicle_id", "part_fitments", ["vehicle_id"])
    op.create_index("ix_part_fitments_part_id", "part_fitments", ["part_id"])


def downgrade():
    connection = op.get_bind()
    if any(connection.scalar(sa.text(f"SELECT COUNT(*) FROM {table}")) for table in ("part_fitments", "vehicle_models")):
        raise RuntimeError("车型或适配资料非空，禁止通过迁移回退删除资料")
    if connection.scalar(sa.text("SELECT COUNT(*) FROM parts WHERE aliases != '' OR manufacturer != '' OR manufacturer_part_number != '' OR specification != '' OR source_reference != '' OR source_version != '' OR reviewed_at IS NOT NULL OR reviewed_by IS NOT NULL")):
        raise RuntimeError("备件已登记身份或来源，禁止通过迁移回退删除这些字段")
    op.drop_table("part_fitments")
    op.drop_table("vehicle_models")
    op.drop_constraint("fk_part_reviewer", "parts", type_="foreignkey")
    for name in ["reviewed_at", "reviewed_by", "source_version", "source_reference", "specification",
        "manufacturer_part_number", "manufacturer", "aliases"]:
        op.drop_column("parts", name)
