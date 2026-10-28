from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from backend.app.inventory import audit, post_opening
from backend.app.models import AuditLog, DocumentLine, Part, PartFitment, StockBalance, StockLedger, Supplier, User, Warehouse
from backend.app.queries import balance_statement, ledger_statement
from backend.app.schemas import Login, OpeningInput, PartInput, SupplierInput, UserCreate, UserUpdate, WarehouseInput
from backend.app.security import administrator, current_user, dummy_hash, issue_token, passwords, session_dependency, stock_writer

router = APIRouter(prefix="/api")


def user_view(user):
    return {key: getattr(user, key) for key in ("id", "username", "display_name", "role", "active")}


def catalog_view(row):
    values = {column.name: getattr(row, column.name) for column in row.__table__.columns}
    return {key: str(value) if isinstance(value, Decimal) else value for key, value in values.items()}


def page_result(session, statement, page, page_size, *, scalars=False):
    total = session.scalar(select(func.count()).select_from(statement.order_by(None).subquery()))
    result = session.execute(statement.offset((page - 1) * page_size).limit(page_size))
    items = [catalog_view(row) for row in result.scalars()] if scalars else [dict(row) for row in result.mappings()]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.post("/auth/login", tags=["认证"])
def login(payload: Login, request: Request, session: Session = Depends(session_dependency)):
    user = session.scalar(select(User).where(User.username == payload.username))
    valid = passwords.verify(payload.password.get_secret_value(), user.password_hash if user else dummy_hash)
    if not valid or user is None or not user.active:
        raise HTTPException(401, "用户名或密码错误")
    token = issue_token(user, request)
    audit(session, user.id, "login", "users", user.id)
    session.commit()
    return {"access_token": token, "token_type": "bearer", "expires_in": 3600, "user": user_view(user)}


@router.get("/auth/me", tags=["认证"])
def me(user: User = Depends(current_user)):
    return user_view(user)


@router.get("/users", tags=["账号"])
def users(user: User = Depends(administrator), session: Session = Depends(session_dependency)):
    return {"items": [user_view(row) for row in session.scalars(select(User).order_by(User.id))]}


@router.post("/users", status_code=201, tags=["账号"])
def create_user(payload: UserCreate, user: User = Depends(administrator), session: Session = Depends(session_dependency)):
    row = User(**payload.model_dump(exclude={"password"}), password_hash=passwords.hash(payload.password.get_secret_value()))
    session.add(row)
    session.flush()
    audit(session, user.id, "user.create", "users", row.id)
    session.commit()
    return user_view(row)


@router.patch("/users/{user_id}", tags=["账号"])
def update_user(user_id: int, payload: UserUpdate, user: User = Depends(administrator), session: Session = Depends(session_dependency)):
    row = session.get(User, user_id)
    if row is None:
        raise HTTPException(404, "账号不存在")
    if row.id == user.id and (payload.role != "admin" or not payload.active):
        raise HTTPException(409, "不能停用或降级当前管理员账号")
    row.role, row.active = payload.role, payload.active
    audit(session, user.id, "user.update", "users", row.id)
    session.commit()
    return user_view(row)


def catalog_list(model, session, q, active, page, page_size):
    code = model.sku if model is Part else model.code
    statement = select(model)
    if q:
        columns = [code, model.name]
        if model is Part:
            columns.extend([Part.aliases, Part.manufacturer_part_number, Part.manufacturer])
        statement = statement.where(or_(*[column.contains(q, autoescape=True) for column in columns]))
    if active is not None:
        statement = statement.where(model.active == active)
    return page_result(session, statement.order_by(model.id), page, page_size, scalars=True)


def save_catalog(model, payload, row_id, session, operator, image_root=None):
    if payload.image_key.startswith('upload-'):
        from backend.app.media import image_path
        from backend.app.config import ROOT
        if not image_path(image_root or ROOT, payload.image_key).is_file():
            raise HTTPException(422, '上传图片不存在，请重新上传或选择内置配图')
    row = session.scalar(select(model).where(model.id == row_id).with_for_update()) if row_id else model()
    if row is None:
        raise HTTPException(404, "记录不存在")
    if model is Part:
        supplier_id = payload.default_supplier_id
        if supplier_id:
            supplier = session.get(Supplier, supplier_id)
            if supplier is None or (not supplier.active and (not row_id or row.default_supplier_id != supplier_id)):
                raise HTTPException(409, "默认供应商不存在或已停用")
            if payload.category not in supplier.supply_categories:
                raise HTTPException(409, "默认供应商不供应该备件类别")
        if row_id and (row.unit != payload.unit or row.sku != payload.sku):
            if session.scalar(select(StockBalance.id).where(StockBalance.part_id == row_id).limit(1).with_for_update()) or session.scalar(select(DocumentLine.id).where(DocumentLine.part_id == row_id).limit(1)) or session.scalar(select(PartFitment.id).where(PartFitment.part_id == row_id).limit(1)):
                raise HTTPException(409, "已被业务或适配关系引用的备件不能修改编码或计量单位")
        identity_fields = ("sku", "name", "unit", "manufacturer", "manufacturer_part_number", "specification", "source_reference", "source_version", "active")
        if row_id and any(getattr(row, key) != getattr(payload, key) for key in identity_fields):
            row.reviewed_at = row.reviewed_by = None
            session.execute(update(PartFitment).where(PartFitment.part_id == row_id)
                .values(reviewed_at=None, reviewed_by=None))
    if model is Supplier and row_id:
        categories = set(payload.supply_categories)
        if any(category not in categories for category in session.scalars(select(Part.category).where(Part.default_supplier_id == row_id))):
            raise HTTPException(409, "缩小供货范围前，请先调整范围外备件的默认供应商")
    for key, value in payload.model_dump().items():
        setattr(row, key, value)
    session.add(row)
    session.flush()
    audit(session, operator.id, "catalog.update" if row_id else "catalog.create", model.__tablename__, row.id)
    session.commit()
    return catalog_view(row)


@router.get("/parts", tags=["基础资料"])
def parts(q: str = Query(default="", max_length=100), active: bool | None = None, page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=100), user: User = Depends(current_user), session: Session = Depends(session_dependency)):
    return catalog_list(Part, session, q, active, page, page_size)


@router.post("/parts", status_code=201, tags=["基础资料"])
def create_part(payload: PartInput, request: Request, user: User = Depends(administrator), session: Session = Depends(session_dependency)):
    return save_catalog(Part, payload, None, session, user, request.app.state.knowledge.root)


@router.put("/parts/{row_id}", tags=["基础资料"])
def update_part(row_id: int, payload: PartInput, request: Request, user: User = Depends(administrator), session: Session = Depends(session_dependency)):
    return save_catalog(Part, payload, row_id, session, user, request.app.state.knowledge.root)


@router.get("/suppliers", tags=["基础资料"])
def suppliers(q: str = Query(default="", max_length=100), active: bool | None = None, page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=100), user: User = Depends(current_user), session: Session = Depends(session_dependency)):
    return catalog_list(Supplier, session, q, active, page, page_size)


@router.post("/suppliers", status_code=201, tags=["基础资料"])
def create_supplier(payload: SupplierInput, request: Request, user: User = Depends(administrator), session: Session = Depends(session_dependency)):
    return save_catalog(Supplier, payload, None, session, user, request.app.state.knowledge.root)


@router.put("/suppliers/{row_id}", tags=["基础资料"])
def update_supplier(row_id: int, payload: SupplierInput, request: Request, user: User = Depends(administrator), session: Session = Depends(session_dependency)):
    return save_catalog(Supplier, payload, row_id, session, user, request.app.state.knowledge.root)


@router.get("/warehouses", tags=["基础资料"])
def warehouses(q: str = Query(default="", max_length=100), active: bool | None = None, page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=100), user: User = Depends(current_user), session: Session = Depends(session_dependency)):
    return catalog_list(Warehouse, session, q, active, page, page_size)


@router.post("/warehouses", status_code=201, tags=["基础资料"])
def create_warehouse(payload: WarehouseInput, request: Request, user: User = Depends(administrator), session: Session = Depends(session_dependency)):
    return save_catalog(Warehouse, payload, None, session, user, request.app.state.knowledge.root)


@router.put("/warehouses/{row_id}", tags=["基础资料"])
def update_warehouse(row_id: int, payload: WarehouseInput, request: Request, user: User = Depends(administrator), session: Session = Depends(session_dependency)):
    return save_catalog(Warehouse, payload, row_id, session, user, request.app.state.knowledge.root)


@router.post("/inventory/openings", tags=["库存"])
def opening(payload: OpeningInput, user: User = Depends(stock_writer), session: Session = Depends(session_dependency)):
    document, replayed = post_opening(session, payload, user.id)
    session.commit()
    return {"id": document.id, "request_id": document.request_id, "status": "posted", "replayed": replayed}


@router.get("/inventory/balances", tags=["库存"])
def balances(q: str = Query(default="", max_length=100), warehouse_id: int | None = Query(default=None, gt=0), page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=100), user: User = Depends(current_user), session: Session = Depends(session_dependency)):
    statement = balance_statement()
    if warehouse_id:
        statement = statement.where(Warehouse.id == warehouse_id)
    if q:
        statement = statement.where(or_(Part.sku.contains(q, autoescape=True), Part.name.contains(q, autoescape=True)))
    return page_result(session, statement.order_by(Warehouse.id, Part.id), page, page_size)


@router.get("/inventory/ledger", tags=["库存"])
def ledger(warehouse_id: int | None = Query(default=None, gt=0), page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=100), user: User = Depends(current_user), session: Session = Depends(session_dependency)):
    statement = ledger_statement()
    if warehouse_id:
        statement = statement.where(StockLedger.warehouse_id == warehouse_id)
    return page_result(session, statement.order_by(StockLedger.id.desc()), page, page_size)


@router.get("/audit", tags=["审计"])
def audit_logs(page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=100), user: User = Depends(administrator), session: Session = Depends(session_dependency)):
    statement = select(AuditLog.id, User.display_name.label("operator"), AuditLog.action, AuditLog.entity, AuditLog.entity_id, AuditLog.result, AuditLog.created_at).join(User, User.id == AuditLog.operator_id).order_by(AuditLog.id.desc())
    return page_result(session, statement, page, page_size)
