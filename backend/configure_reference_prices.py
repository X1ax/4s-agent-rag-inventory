"""Explicitly fill missing project reference prices, preserving existing prices/history."""
import json
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.config import ROOT, Settings
from backend.app.db import make_engine
from backend.app.inventory import audit
from backend.app.models import Part, User


def fill_missing(session, operator_id, rows):
    changes, preserved = [], []
    for item in rows:
        price = Decimal(item['reference_price'])
        if not price.is_finite() or not 0 < price < Decimal('10000000000') or price != price.quantize(Decimal('.01')):
            raise ValueError('Reference prices must be positive decimal amounts with at most two decimal places')
        part = session.scalar(select(Part).where(Part.sku == item['sku']).with_for_update())
        if part is None or part.name != item['name']:
            raise ValueError('Registered SKU/name mismatch; do not overwrite unrelated records')
        if part.reference_price != 0:
            preserved.append({'sku': part.sku, 'reference_price': str(part.reference_price)})
            continue
        changes.append({'part_id': part.id, 'sku': part.sku, 'name': part.name, 'before': str(part.reference_price), 'after': str(price)})
        part.reference_price = price
        audit(session, operator_id, 'part.reference_price.configure', 'parts', part.id)
    return {'changes': changes, 'preserved': preserved}


def main():
    settings = Settings()
    if settings.mysql_database != 'fours_agent_dev' or settings.mysql_user != 'fours_dev':
        raise RuntimeError('Restricted to fours_agent_dev / fours_dev')
    policy = json.loads((ROOT/'data/catalog/parts-expansion.json').read_text(encoding='utf-8'))
    if {r['sku'] for r in policy['parts']} != {f'CAT-{i:03}' for i in range(13,49)} or len(policy['parts']) != 36:
        raise ValueError('Expected exactly the registered 36 generic component types')
    engine = make_engine(settings)
    try:
        with Session(engine) as session, session.begin():
            operator = session.scalar(select(User).where(User.username=='demo_admin',User.active.is_(True),User.role=='admin'))
            if operator is None:
                raise RuntimeError('An existing active project administrator is required')
            result = fill_missing(session, operator.id, policy['parts'])
            if result['changes']:
                backup = ROOT/'data/local-backups'/('reference-prices-'+datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')+'.json')
                backup.parent.mkdir(parents=True,exist_ok=True)
                backup.write_text(json.dumps({'policy_version':policy['version'],'source_notice':policy['reference_price_notice'],**result},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        print(json.dumps({'configured':len(result['changes']),'preserved':len(result['preserved']),'policy_version':policy['version'],'stock_or_historical_prices_changed':False},ensure_ascii=False))
    finally:
        engine.dispose()


if __name__=='__main__':
    main()
