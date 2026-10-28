from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.inventory import audit
from backend.app.models import Part, PriceChange, Supplier, SupplierPrice, User, utcnow
from backend.app.schemas import Input
from backend.app.security import administrator, current_user, session_dependency

router = APIRouter(prefix='/api/pricing', tags=['价格管理'])


class SupplyPriceInput(Input):
    supply_price: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    source: str = Field(min_length=1, max_length=300)
    active: bool = Field(strict=True)
    expected_version: int = Field(strict=True, ge=0)


class SalePriceInput(Input):
    sale_price: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    source: str = Field(min_length=1, max_length=300)
    expected_version: int = Field(strict=True, ge=0)


def price_editor(user: User = Depends(current_user)):
    if user.role not in {'admin', 'manager'}:
        raise HTTPException(403, '当前账号没有门店定价权限')
    return user


def pricing_parts(session, *, part_id=None, supplier_id=None):
    statement = select(Part).order_by(Part.id)
    if part_id:
        statement = statement.where(Part.id == part_id)
    parts = list(session.scalars(statement))
    if part_id and not parts:
        raise HTTPException(404, '备件不存在')
    suppliers = list(session.scalars(select(Supplier).where(Supplier.active == True).order_by(Supplier.id)))
    if supplier_id:
        suppliers = [row for row in suppliers if row.id == supplier_id]
    quotes = {(row.supplier_id, row.part_id): row for row in session.scalars(select(SupplierPrice).where(
        SupplierPrice.part_id.in_([part.id for part in parts])))}
    items = []
    for part in parts:
        offers = []
        for supplier in suppliers:
            if part.category not in supplier.supply_categories:
                continue
            quote = quotes.get((supplier.id, part.id))
            offers.append({'id': supplier.id, 'name': supplier.name,
                'supply_price': format(quote.supply_price, '.2f') if quote else None,
                'version': quote.version if quote else 0, 'active': quote.active if quote else False,
                'source': quote.source if quote else '', 'updated_at': quote.updated_at if quote else None})
        offers.sort(key=lambda row: (row['id'] != part.default_supplier_id, row['id']))
        if supplier_id and not offers:
            continue
        items.append({'part_id': part.id, 'sku': part.sku, 'name': part.name,
            'default_supplier_id': part.default_supplier_id,
            'sale_price': format(part.sale_price, '.2f') if part.sale_price is not None else None,
            'sale_price_version': part.sale_price_version, 'suppliers': offers})
    return {'items': items}


@router.get('/parts')
def parts(part_id: int | None = Query(default=None, gt=0), supplier_id: int | None = Query(default=None, gt=0),
    user: User = Depends(current_user), session: Session = Depends(session_dependency)):
    return pricing_parts(session, part_id=part_id, supplier_id=supplier_id)


@router.put('/suppliers/{supplier_id}/parts/{part_id}')
def update_supply_price(supplier_id: int, part_id: int, payload: SupplyPriceInput,
    user: User = Depends(administrator), session: Session = Depends(session_dependency)):
    supplier = session.scalar(select(Supplier).where(Supplier.id == supplier_id).with_for_update())
    part = session.scalar(select(Part).where(Part.id == part_id).with_for_update())
    if supplier is None or part is None:
        raise HTTPException(404, '供应商或备件不存在')
    if not supplier.active or not part.active or part.category not in supplier.supply_categories:
        raise HTTPException(409, '供应商不具备该备件的当前供货资格')
    quote = session.scalar(select(SupplierPrice).where(SupplierPrice.supplier_id == supplier_id,
        SupplierPrice.part_id == part_id).with_for_update())
    if payload.expected_version != (quote.version if quote else 0):
        raise HTTPException(409, '报价已变更，请刷新后重新提交')
    before = quote.supply_price if quote else None
    if quote is None:
        quote = SupplierPrice(supplier_id=supplier_id, part_id=part_id, version=0)
        session.add(quote)
    quote.supply_price, quote.source, quote.active = payload.supply_price, payload.source, payload.active
    quote.version += 1
    quote.updated_at = utcnow()
    session.add(PriceChange(part_id=part_id, supplier_id=supplier_id, kind='supply', before_price=before,
        after_price=payload.supply_price, source=payload.source, operator_id=user.id))
    session.flush()
    audit(session, user.id, 'price.supply', 'supplier_prices', quote.id)
    session.commit()
    return {'id': quote.id, 'supplier_id': supplier_id, 'part_id': part_id,
        'supply_price': format(quote.supply_price, '.2f'), 'version': quote.version,
        'active': quote.active, 'source': quote.source, 'updated_at': quote.updated_at}


@router.patch('/parts/{part_id}/sale-price')
def update_sale_price(part_id: int, payload: SalePriceInput, user: User = Depends(price_editor),
    session: Session = Depends(session_dependency)):
    part = session.scalar(select(Part).where(Part.id == part_id).with_for_update())
    if part is None:
        raise HTTPException(404, '备件不存在')
    if payload.expected_version != part.sale_price_version:
        raise HTTPException(409, '门店定价已变更，请刷新后重新提交')
    session.add(PriceChange(part_id=part_id, kind='sale', before_price=part.sale_price,
        after_price=payload.sale_price, source=payload.source, operator_id=user.id))
    part.sale_price = payload.sale_price
    part.sale_price_version += 1
    audit(session, user.id, 'price.sale', 'parts', part.id)
    session.commit()
    return {'part_id': part.id, 'sale_price': format(part.sale_price, '.2f'),
        'sale_price_version': part.sale_price_version}
