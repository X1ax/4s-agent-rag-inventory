from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, ForeignKey, Integer, JSON, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db import Base


def utcnow():
    return datetime.now(UTC).replace(tzinfo=None)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("role IN ('admin', 'storekeeper', 'manager')", name="ck_user_role"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(40), unique=True)
    display_name: Mapped[str] = mapped_column(String(80))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Supplier(Base):
    __tablename__ = "suppliers"
    __table_args__ = (CheckConstraint("lead_time_days >= 0", name="ck_supplier_lead_time"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    contact: Mapped[str] = mapped_column(String(80), default="")
    phone: Mapped[str] = mapped_column(String(40), default="")
    lead_time_days: Mapped[int] = mapped_column(Integer, default=7)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    image_key: Mapped[str] = mapped_column(String(40), default="", server_default="")
    supply_categories: Mapped[list[str]] = mapped_column(JSON, default=list)


class Warehouse(Base):
    __tablename__ = "warehouses"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    location: Mapped[str] = mapped_column(String(200), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    image_key: Mapped[str] = mapped_column(String(40), default="", server_default="")


class SupplierPrice(Base):
    __tablename__ = 'supplier_prices'
    __table_args__ = (UniqueConstraint('supplier_id', 'part_id', name='uq_supplier_part_price'),
        CheckConstraint('supply_price >= 0 AND version > 0', name='ck_supplier_price'))
    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey('suppliers.id'))
    part_id: Mapped[int] = mapped_column(ForeignKey('parts.id'))
    supply_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    version: Mapped[int] = mapped_column(Integer, default=1)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    source: Mapped[str] = mapped_column(String(300))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PriceChange(Base):
    __tablename__ = 'price_changes'
    id: Mapped[int] = mapped_column(primary_key=True)
    part_id: Mapped[int] = mapped_column(ForeignKey('parts.id'))
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey('suppliers.id'))
    kind: Mapped[str] = mapped_column(String(20))
    before_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    after_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    source: Mapped[str] = mapped_column(String(300))
    operator_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PricingBackfill(Base):
    __tablename__ = 'pricing_backfills'
    version: Mapped[str] = mapped_column(String(60), primary_key=True)
    plan_digest: Mapped[str] = mapped_column(String(64))
    quantity_fingerprint: Mapped[str] = mapped_column(String(64))
    summary: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Part(Base):
    __tablename__ = "parts"
    __table_args__ = (
        CheckConstraint("safety_stock >= 0", name="ck_part_safety"),
        CheckConstraint("reference_price >= 0", name="ck_part_price"),
        CheckConstraint("sale_price IS NULL OR sale_price >= 0", name="ck_part_sale_price"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    sku: Mapped[str] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    category: Mapped[str] = mapped_column(String(40), default="")
    unit: Mapped[str] = mapped_column(String(10))
    safety_stock: Mapped[int] = mapped_column(Integer, default=0)
    reference_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    sale_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    sale_price_version: Mapped[int] = mapped_column(Integer, default=0, server_default='0')
    default_supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id"))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    image_key: Mapped[str] = mapped_column(String(40), default="", server_default="")
    aliases: Mapped[str] = mapped_column(String(200), default="", server_default="")
    manufacturer: Mapped[str] = mapped_column(String(100), default="", server_default="")
    manufacturer_part_number: Mapped[str] = mapped_column(String(100), default="", server_default="")
    specification: Mapped[str] = mapped_column(String(200), default="", server_default="")
    source_reference: Mapped[str] = mapped_column(String(300), default="", server_default="")
    source_version: Mapped[str] = mapped_column(String(60), default="", server_default="")
    reviewed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)


class VehicleModel(Base):
    __tablename__ = "vehicle_models"
    __table_args__ = (CheckConstraint("model_year IS NULL OR model_year BETWEEN 1900 AND 2100", name="ck_vehicle_year"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True)
    brand: Mapped[str] = mapped_column(String(80), default="")
    model_name: Mapped[str] = mapped_column(String(100))
    model_year: Mapped[int | None] = mapped_column(Integer)
    powertrain_type: Mapped[str] = mapped_column(String(60), default="")
    engine_code: Mapped[str] = mapped_column(String(100), default="")
    transmission: Mapped[str] = mapped_column(String(100), default="")
    market: Mapped[str] = mapped_column(String(80), default="")
    configuration: Mapped[str] = mapped_column(String(300), default="")
    source_reference: Mapped[str] = mapped_column(String(300), default="")
    source_version: Mapped[str] = mapped_column(String(60), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    reviewed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)


class PartFitment(Base):
    __tablename__ = "part_fitments"
    __table_args__ = (CheckConstraint("required_quantity IS NULL OR required_quantity > 0", name="ck_fitment_quantity"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    vehicle_id: Mapped[int] = mapped_column(ForeignKey("vehicle_models.id"), index=True)
    part_id: Mapped[int] = mapped_column(ForeignKey("parts.id"), index=True)
    conditions: Mapped[str] = mapped_column(String(500), default="")
    restrictions: Mapped[str] = mapped_column(String(500), default="")
    required_quantity: Mapped[int | None] = mapped_column(Integer)
    source_reference: Mapped[str] = mapped_column(String(300), default="")
    source_version: Mapped[str] = mapped_column(String(60), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    reviewed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)


class OpeningDocument(Base):
    __tablename__ = "opening_documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[str] = mapped_column(String(36), unique=True)
    payload_hash: Mapped[str] = mapped_column(String(64))
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"))
    operator_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    business_date: Mapped[date] = mapped_column(Date)
    note: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class OpeningLine(Base):
    __tablename__ = "opening_lines"
    __table_args__ = (
        UniqueConstraint("document_id", "part_id", name="uq_opening_document_part"),
        CheckConstraint("quantity > 0", name="ck_opening_quantity"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("opening_documents.id"))
    part_id: Mapped[int] = mapped_column(ForeignKey("parts.id"))
    quantity: Mapped[int] = mapped_column(Integer)
    unit_cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    cost_source: Mapped[str] = mapped_column(String(300), default='', server_default='')


class StockBalance(Base):
    __tablename__ = "stock_balances"
    __table_args__ = (
        UniqueConstraint("warehouse_id", "part_id", name="uq_balance_warehouse_part"),
        CheckConstraint("quantity >= 0", name="ck_balance_quantity"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"))
    part_id: Mapped[int] = mapped_column(ForeignKey("parts.id"))
    quantity: Mapped[int] = mapped_column(Integer)
    stock_value: Mapped[Decimal | None] = mapped_column(Numeric(24, 2))


class StockLedger(Base):
    __tablename__ = "stock_ledger"
    __table_args__ = (
        CheckConstraint("quantity_delta != 0", name="ck_ledger_delta"),
        CheckConstraint("(opening_line_id IS NOT NULL AND document_line_id IS NULL) OR (opening_line_id IS NULL AND document_line_id IS NOT NULL)", name="ck_ledger_source"),
        UniqueConstraint("document_line_id", "event", name="uq_ledger_line_event"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"))
    part_id: Mapped[int] = mapped_column(ForeignKey("parts.id"))
    opening_line_id: Mapped[int | None] = mapped_column(ForeignKey("opening_lines.id"), unique=True)
    document_line_id: Mapped[int | None] = mapped_column(ForeignKey("document_lines.id"))
    operator_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    event: Mapped[str] = mapped_column(String(30), default="opening", server_default="opening")
    business_date: Mapped[date | None] = mapped_column(Date)
    quantity_delta: Mapped[int] = mapped_column(Integer)
    value_delta: Mapped[Decimal | None] = mapped_column(Numeric(24, 2))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    operator_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(40))
    entity: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[int] = mapped_column(Integer)
    result: Mapped[str] = mapped_column(String(20), default="success")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint("(kind = 'purchase' AND status IN ('draft','confirmed','partial','completed','cancelled')) OR (kind = 'receipt' AND status = 'posted') OR (kind = 'issue' AND status IN ('draft','posted','cancelled')) OR (kind = 'transfer' AND status IN ('draft','sent','received','cancelled'))", name="ck_document_state"),
        CheckConstraint("kind != 'transfer' OR (target_warehouse_id IS NOT NULL AND target_warehouse_id != warehouse_id)", name="ck_transfer_target"),
        CheckConstraint("kind != 'receipt' OR purchase_id IS NOT NULL", name="ck_receipt_purchase"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="draft")
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"))
    target_warehouse_id: Mapped[int | None] = mapped_column(ForeignKey("warehouses.id"))
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id"))
    purchase_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"))
    operator_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    business_date: Mapped[date] = mapped_column(Date)
    note: Mapped[str] = mapped_column(String(200), default="")
    issue_type: Mapped[str | None] = mapped_column(String(20))
    warranty_reason: Mapped[str] = mapped_column(String(200), default='', server_default='')
    work_order: Mapped[str] = mapped_column(String(80), default='', server_default='')
    pricing_source: Mapped[str] = mapped_column(String(100), default='', server_default='')
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class DocumentReview(Base):
    __tablename__ = "document_reviews"
    __table_args__ = (CheckConstraint("status IN ('pending','approved','rejected')", name="ck_document_review_status"),)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), primary_key=True)
    status: Mapped[str] = mapped_column(String(20), index=True)
    submitted_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    submitted_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    reviewer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    note: Mapped[str] = mapped_column(String(500), default="")


class DocumentLine(Base):
    __tablename__ = "document_lines"
    __table_args__ = (
        UniqueConstraint("document_id", "part_id", name="uq_document_part"),
        CheckConstraint("quantity > 0 AND received_quantity >= 0 AND received_quantity <= quantity", name="ck_document_line_quantity"),
        CheckConstraint("unit_price >= 0", name="ck_document_line_price"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    part_id: Mapped[int] = mapped_column(ForeignKey("parts.id"))
    quantity: Mapped[int] = mapped_column(Integer)
    received_quantity: Mapped[int] = mapped_column(Integer, default=0)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    sale_unit_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    cost_amount: Mapped[Decimal | None] = mapped_column(Numeric(24, 2))
    quote_version: Mapped[int | None] = mapped_column(Integer)
    sale_price_version: Mapped[int | None] = mapped_column(Integer)
    pricing_source: Mapped[str] = mapped_column(String(100), default='', server_default='')
    purchase_line_id: Mapped[int | None] = mapped_column(ForeignKey("document_lines.id"))
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id"))


class OperationRequest(Base):
    __tablename__ = "operation_requests"
    request_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    payload_hash: Mapped[str] = mapped_column(String(64))
    operator_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    result_status: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AgentProposal(Base):
    __tablename__ = "agent_proposals"
    __table_args__ = (UniqueConstraint("thread_id", "turn_id", name="uq_agent_proposal_turn"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    thread_id: Mapped[str] = mapped_column(String(32), index=True)
    turn_id: Mapped[str] = mapped_column(String(32))
    version: Mapped[int] = mapped_column(Integer, default=1)
    action: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(JSON)
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    summary: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    result: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
