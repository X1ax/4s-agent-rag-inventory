from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from backend.app.api import catalog_view, page_result
from backend.app.inventory import audit
from backend.app.models import Part, PartFitment, User, VehicleModel, utcnow
from backend.app.schemas import FitmentInput, VehicleInput
from backend.app.security import administrator, current_user, session_dependency

router = APIRouter(prefix="/api", tags=["车型与适配"])


def locked(session, model, row_id):
    row = session.scalar(select(model).where(model.id == row_id).with_for_update().execution_options(populate_existing=True))
    if row is None:
        raise HTTPException(404, "资料不存在")
    return row


@router.get("/vehicle-models")
def vehicles(q: str = Query(default="", max_length=100), page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100), active: bool | None = None,
    user: User = Depends(current_user), session: Session = Depends(session_dependency)):
    statement = select(VehicleModel)
    if q:
        statement = statement.where(or_(*[column.contains(q, autoescape=True) for column in [
            VehicleModel.code, VehicleModel.brand, VehicleModel.model_name, VehicleModel.engine_code,
            VehicleModel.powertrain_type, VehicleModel.transmission, VehicleModel.market]]))
    if active is not None:
        statement = statement.where(VehicleModel.active == active)
    return page_result(session, statement.order_by(VehicleModel.id), page, page_size, scalars=True)


def save_vehicle(payload, row_id, session, user):
    row = locked(session, VehicleModel, row_id) if row_id else VehicleModel()
    changed = any(getattr(row, key, None) != value for key, value in payload.model_dump().items())
    if changed:
        row.reviewed_at = row.reviewed_by = None
        if row_id:
            session.execute(update(PartFitment).where(PartFitment.vehicle_id == row_id)
                .values(reviewed_at=None, reviewed_by=None))
    for key, value in payload.model_dump().items():
        setattr(row, key, value)
    session.add(row)
    session.flush()
    audit(session, user.id, "catalog.update" if row_id else "catalog.create", "vehicle_models", row.id)
    session.commit()
    return catalog_view(row)


@router.post("/vehicle-models", status_code=201)
def create_vehicle(payload: VehicleInput, user: User = Depends(administrator), session: Session = Depends(session_dependency)):
    return save_vehicle(payload, None, session, user)


@router.put("/vehicle-models/{row_id}")
def update_vehicle(row_id: int, payload: VehicleInput, user: User = Depends(administrator), session: Session = Depends(session_dependency)):
    return save_vehicle(payload, row_id, session, user)


@router.get("/fitments")
def fitments(vehicle_id: int | None = Query(default=None, gt=0), part_id: int | None = Query(default=None, gt=0),
    reviewed: bool | None = None, page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=100),
    user: User = Depends(current_user), session: Session = Depends(session_dependency)):
    eligible = and_(PartFitment.reviewed_at.is_not(None), Part.reviewed_at.is_not(None),
        VehicleModel.reviewed_at.is_not(None), PartFitment.active, Part.active, VehicleModel.active)
    statement = select(*PartFitment.__table__.columns, Part.sku, Part.name.label("part_name"), Part.unit,
        Part.manufacturer_part_number, VehicleModel.code.label("vehicle_code"), VehicleModel.model_name,
        VehicleModel.model_year, VehicleModel.powertrain_type, VehicleModel.engine_code,
        VehicleModel.transmission, VehicleModel.market, VehicleModel.configuration,
        eligible.label("eligible")).join(Part, Part.id == PartFitment.part_id)
    statement = statement.join(VehicleModel, VehicleModel.id == PartFitment.vehicle_id)
    if vehicle_id is not None:
        statement = statement.where(PartFitment.vehicle_id == vehicle_id)
    if part_id is not None:
        statement = statement.where(PartFitment.part_id == part_id)
    if reviewed is not None:
        statement = statement.where(eligible == reviewed)
    return page_result(session, statement.order_by(PartFitment.id), page, page_size)


def save_fitment(payload, row_id, session, user):
    # Parent locks also serialize review with edits to the supporting identities.
    vehicle = locked(session, VehicleModel, payload.vehicle_id)
    part = locked(session, Part, payload.part_id)
    if not vehicle.active or not part.active:
        raise HTTPException(409, "请选择启用的车型和备件")
    row = locked(session, PartFitment, row_id) if row_id else PartFitment()
    if any(getattr(row, key, None) != value for key, value in payload.model_dump().items()):
        row.reviewed_at = row.reviewed_by = None
    for key, value in payload.model_dump().items():
        setattr(row, key, value)
    session.add(row)
    session.flush()
    audit(session, user.id, "catalog.update" if row_id else "catalog.create", "part_fitments", row.id)
    session.commit()
    return catalog_view(row)


@router.post("/fitments", status_code=201)
def create_fitment(payload: FitmentInput, user: User = Depends(administrator), session: Session = Depends(session_dependency)):
    return save_fitment(payload, None, session, user)


@router.put("/fitments/{row_id}")
def update_fitment(row_id: int, payload: FitmentInput, user: User = Depends(administrator), session: Session = Depends(session_dependency)):
    return save_fitment(payload, row_id, session, user)


@router.post("/catalog/{kind}/{row_id}/review")
def review(kind: Literal["parts", "vehicle-models", "fitments"], row_id: int,
    user: User = Depends(administrator), session: Session = Depends(session_dependency)):
    model = {"parts": Part, "vehicle-models": VehicleModel, "fitments": PartFitment}[kind]
    if model is PartFitment:
        reference = session.get(PartFitment, row_id)
        if reference is None:
            raise HTTPException(404, "资料不存在")
        vehicle = locked(session, VehicleModel, reference.vehicle_id)
        part = locked(session, Part, reference.part_id)
        row = locked(session, model, row_id)
        if row.vehicle_id != vehicle.id or row.part_id != part.id:
            raise HTTPException(409, "适配资料已更新，请刷新后重新核准")
        if not all(item.active and item.reviewed_at for item in (vehicle, part)):
            raise HTTPException(409, "车型和备件身份须先完成人工核准")
        fields = ["conditions", "restrictions", "required_quantity", "source_reference", "source_version"]
    else:
        row = locked(session, model, row_id)
        fields = (["manufacturer", "manufacturer_part_number", "specification", "source_reference", "source_version"]
            if model is Part else ["brand", "model_year", "powertrain_type", "engine_code", "transmission",
                "market", "configuration", "source_reference", "source_version"])
    if not row.active or not all(getattr(row, key) for key in fields):
        raise HTTPException(409, "资料未启用或身份、条件、数量及来源版本不完整，不能核准")
    if row.reviewed_at is None:
        row.reviewed_at, row.reviewed_by = utcnow().replace(microsecond=0), user.id
        audit(session, user.id, "catalog.review", model.__tablename__, row.id)
        session.commit()
    return catalog_view(row)
