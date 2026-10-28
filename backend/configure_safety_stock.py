"""Explicitly fill missing CAT thresholds in the project dev DB; never alter stock."""
import json
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.config import ROOT, Settings
from backend.app.db import make_engine
from backend.app.inventory import audit
from backend.app.models import Part, User


def fill_missing(session, operator_id, rows):
    changes, preserved = [], []
    for item in rows:
        if not item['sku'].startswith('CAT-') or type(item['safety_stock']) is not int or not 1 <= item['safety_stock'] <= 4:
            raise ValueError('Only registered CAT parts with planned thresholds 1..4 are allowed')
        part = session.scalar(select(Part).where(Part.sku == item['sku']).with_for_update())
        if part is None:
            raise ValueError('Run the registered catalog import first')
        if part.safety_stock != 0:
            preserved.append({'sku': part.sku, 'safety_stock': part.safety_stock})
            continue
        changes.append({'part_id': part.id, 'sku': part.sku, 'name': part.name, 'before': 0, 'after': item['safety_stock']})
        part.safety_stock = item['safety_stock']
        audit(session, operator_id, 'part.safety_stock.configure', 'parts', part.id)
    return {'changes': changes, 'preserved': preserved}


def main():
    settings = Settings()
    if settings.mysql_database != 'fours_agent_dev' or settings.mysql_user != 'fours_dev':
        raise RuntimeError('Restricted to fours_agent_dev / fours_dev')
    policy = json.loads((ROOT / 'data/catalog/safety-stock-policy.json').read_text(encoding='utf-8'))
    if len(policy['parts']) != 36 or len({r['sku'] for r in policy['parts']}) != 36:
        raise ValueError('Expected the complete registered 36-part policy')
    engine = make_engine(settings)
    try:
        with Session(engine) as session, session.begin():
            operator = session.scalar(select(User).where(User.username == 'demo_admin', User.active == True, User.role == 'admin'))
            if operator is None:
                raise RuntimeError('An existing active project administrator is required')
            result = fill_missing(session, operator.id, policy['parts'])
            if result['changes']:
                backup = ROOT / 'data/local-backups' / ('safety-stock-' + datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ') + '.json')
                backup.parent.mkdir(parents=True, exist_ok=True)
                backup.write_text(json.dumps({'policy_version': policy['version'], **result}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({'configured': len(result['changes']), 'preserved': len(result['preserved']),
            'quantity_or_price_changed': False, 'policy_version': policy['version']}, ensure_ascii=False))
    finally:
        engine.dispose()


if __name__ == '__main__':
    main()
