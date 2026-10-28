"""Explicit alternate simulated repair mix; never called by application startup.

Retain the original seeded <5% warranty cases, reclassify other eligible synthetic
warranty cases as paid repairs. This is a scenario assumption, not an industry rate.
"""
import argparse
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.config import ROOT, Settings
from backend.app.db import make_engine
from backend.app.models import AuditLog, Document, DocumentLine, PricingBackfill, User
from backend.backfill_pricing import (SOURCE as ORIGINAL_SOURCE, candidate_price, digest,
    fixed_index, quantity_fingerprint, snapshot)

VERSION = 'simulated_issue_mix_20261026_v1'
SOURCE = '模拟维修结构调整20261026-v1；收费为主情景'


def make_plan(session):
    before = snapshot(session)
    parts = {row['id']: row for row in before['parts']}
    lines = {}
    for line in before['document_lines']:
        lines.setdefault(line['document_id'], []).append(line)
    changes = []
    counts = Counter(row['issue_type'] for row in before['documents']
        if row['kind'] == 'issue' and row['status'] == 'posted')
    converted = 0
    for row in before['documents']:
        if (row['kind'] != 'issue' or row['status'] != 'posted' or row['issue_type'] != 'warranty'
                or row['pricing_source'] != ORIGINAL_SOURCE or fixed_index(f"issue:{row['id']}", 100) < 5):
            continue
        items = lines.get(row['id'], [])
        # Preserve any record no longer wholly owned by the original simulated backfill.
        if not items or any(line['pricing_source'] != ORIGINAL_SOURCE for line in items):
            continue
        if any(line['sale_unit_price'] is None or Decimal(line['sale_unit_price']) != 0
                or line['cost_amount'] is None for line in items):
            raise ValueError('Incomplete original simulated warranty settlement/cost')
        changes.append({'table': 'documents', 'id': row['id'], 'after': {
            'issue_type': 'paid_repair', 'warranty_reason': '', 'pricing_source': SOURCE}})
        for line in items:
            part = parts[line['part_id']]
            if part['sale_price'] is None or Decimal(part['sale_price']) <= 0:
                raise ValueError('Missing positive simulated retail anchor')
            price = candidate_price(part['sale_price'],
                f"{part['sku']}:{row['business_date'][:7]}:historical_retail", 97, 103)
            changes.append({'table': 'document_lines', 'id': line['id'],
                'after': {'sale_unit_price': str(price), 'pricing_source': SOURCE}})
        converted += 1
    summary = {'source': SOURCE, 'converted_documents': converted,
        'converted_lines': len(changes) - converted,
        'paid_documents_before': counts['paid_repair'], 'warranty_documents_before': counts['warranty'],
        'paid_documents_after': counts['paid_repair'] + converted,
        'warranty_documents_after': counts['warranty'] - converted,
        'notice': '用户指定的收费为主模拟情景；非实际门店收费或行业质保比例；数量和成本不变'}
    plan = {'version': VERSION, 'baseline': before, 'baseline_digest': digest(before),
        'quantity_fingerprint': quantity_fingerprint(before), 'changes': changes, 'summary': summary}
    return plan | {'digest': digest(plan)}


def apply_plan(session, plan, operator_id):
    if plan.get('version') != VERSION or plan.get('digest') != digest({k: v for k, v in plan.items() if k != 'digest'}):
        raise ValueError('Invalid scenario plan version/digest')
    operator = session.get(User, operator_id)
    if operator is None or not operator.active or operator.role != 'admin':
        raise ValueError('Active administrator required')
    previous = session.get(PricingBackfill, VERSION)
    if previous is not None:
        if previous.plan_digest != plan['digest']:
            raise ValueError('Scenario version already applied with a different digest')
        return previous.summary
    document_ids = [row['id'] for row in plan['changes'] if row['table'] == 'documents']
    if document_ids:
        session.scalars(select(Document).where(Document.id.in_(document_ids)).order_by(Document.id).with_for_update()).all()
        session.scalars(select(DocumentLine).where(DocumentLine.document_id.in_(document_ids))
            .order_by(DocumentLine.id).with_for_update()).all()
    if make_plan(session) != plan:
        raise ValueError('Scenario baseline changed; regenerate and review the plan')
    models = {'documents': Document, 'document_lines': DocumentLine}
    with session.begin_nested():
        for item in plan['changes']:
            row = session.get(models[item['table']], item['id'])
            for key, value in item['after'].items():
                setattr(row, key, Decimal(value) if key == 'sale_unit_price' else value)
        session.add(PricingBackfill(version=VERSION, plan_digest=plan['digest'],
            quantity_fingerprint=plan['quantity_fingerprint'], summary=plan['summary']))
        session.add(AuditLog(operator_id=operator_id, action='simulated_issue_mix',
            entity='pricing_backfills', entity_id=0))
        session.flush()
    return plan['summary']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--plan', type=Path)
    mode.add_argument('--apply', type=Path)
    args = parser.parse_args()
    settings = Settings()
    if settings.mysql_database != 'fours_agent_dev' or settings.mysql_user != 'fours_dev':
        raise RuntimeError('Restricted to fours_agent_dev / fours_dev')
    engine = make_engine(settings)
    try:
        with Session(engine) as session, session.begin():
            if args.plan:
                plan = make_plan(session)
                args.plan.parent.mkdir(parents=True, exist_ok=True)
                with args.plan.open('x', encoding='utf-8') as handle:
                    json.dump(plan, handle, ensure_ascii=False, indent=2)
                result = plan['summary']
            else:
                plan = json.loads(args.apply.read_text(encoding='utf-8'))
                operator = session.scalar(select(User).where(User.username == 'demo_admin',
                    User.role == 'admin', User.active == True))
                if operator is None:
                    raise ValueError('Expected active project administrator')
                backup = ROOT/'data/local-backups'/f"{VERSION}-{plan['digest'][:12]}.json"
                backup.parent.mkdir(parents=True, exist_ok=True)
                if not backup.exists():
                    with backup.open('x', encoding='utf-8') as handle:
                        json.dump({'version': VERSION, 'digest': plan['digest'], 'baseline': plan['baseline']},
                            handle, ensure_ascii=False, indent=2)
                result = apply_plan(session, plan, operator.id)
        print(json.dumps(result, ensure_ascii=False))
    finally:
        engine.dispose()


if __name__ == '__main__':
    main()
