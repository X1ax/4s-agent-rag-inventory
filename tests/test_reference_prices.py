from decimal import Decimal

import pytest
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from backend.app.models import Part, StockLedger, User, AuditLog
from backend.configure_reference_prices import fill_missing
from test_m1 import system, resources


def test_fill_missing_prices_preserves_nonzero_and_replay_does_not_add_audit(system):
    _,parts=resources(system,2)
    with Session(system[2]) as session, session.begin():
        uid=session.scalar(select(User.id).where(User.username==system[3].lower()+'_admin'))
        session.get(Part,parts[0]['id']).reference_price=Decimal('0')
        policy=[{'sku':p['sku'],'name':p['name'],'reference_price':'85.50'}for p in parts]
        first=fill_missing(session,uid,policy)
        assert len(first['changes'])==1 and len(first['preserved'])==1
        assert session.get(Part,parts[0]['id']).reference_price==Decimal('85.50')
        assert session.get(Part,parts[1]['id']).reference_price==Decimal('12.30')
        assert not fill_missing(session,uid,policy)['changes']
        assert session.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action=='part.reference_price.configure',AuditLog.entity_id==parts[0]['id']))==1
        assert session.scalar(select(func.count()).select_from(StockLedger).where(StockLedger.part_id.in_([p['id']for p in parts])))==0
        for price in ['NaN','-1','1.001','10000000000']:
            with pytest.raises(ValueError):fill_missing(session,uid,[policy[0]|{'reference_price':price}])
