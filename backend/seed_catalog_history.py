"""Reproducible, varied simulated records for catalog/warehouse pairs without issues."""
import hashlib
import json
import random
from datetime import date, timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.config import ROOT, Settings
from backend.app.db import make_engine
from backend.app.documents import create_document, lock_catalogs, receive_purchase, transition
from backend.seed_reviews import review_simulated_document
from backend.app.models import Document, DocumentLine, Part, StockBalance, Supplier, User, Warehouse
from backend.app.schemas import ActionInput, DocumentInput, ReceiptInput
from backend.seed_demo import existing_simulated_document, register_simulated_prices

SEED = 20261024
START = date(2026, 8, 25)
DAYS = 60
END = START + timedelta(days=DAYS-1)
NOTE = '目录随机模拟历史 v1'


def price_range(name):
    ranges = [('压缩机', (900, 2800)), ('发电机', (600, 1800)), ('起动机', (400, 1300)),
        ('半轴', (500, 1400)), ('制动钳', (450, 1400)), ('传感器', (65, 380)),
        ('滤清器', (35, 120)), ('点火线圈', (90, 280)), ('制动片', (150, 480)),
        ('制动盘', (180, 650)), ('皮带', (45, 180)), ('水管', (35, 120)),
        ('散热器', (280, 1100)), ('冷凝器', (280, 1100)), ('减振器', (260, 900)),
        ('控制臂', (220, 850)), ('泵', (220, 900)), ('电机', (150, 650))]
    return next((values for word, values in ranges if word in name), (80, 450))


def build_plan(parts, warehouses, suppliers, issued_pairs, balances, *, seed=SEED):
    plan = []
    for part in sorted(parts, key=lambda r: r.sku):
        eligible = sorted([s for s in suppliers if s.active and part.category in s.supply_categories], key=lambda s: s.code)
        for warehouse in sorted(warehouses, key=lambda w: w.code):
            key = (warehouse.id, part.id)
            if key in issued_pairs:
                continue
            if not eligible:
                raise ValueError(f'No qualified supplier for {part.sku}; no records imported')
            rng = random.Random(int(hashlib.sha256(f'{seed}:{part.sku}:{warehouse.code}'.encode()).hexdigest(), 16))
            frequent = any(word in part.name for word in ('滤清器', '制动片', '皮带', '点火线圈'))
            slow = any(word in part.name for word in ('总成', '压缩机', '发电机', '起动机', '控制臂', '半轴'))
            count = rng.randint(5, 9) if frequent else rng.randint(1, 3) if slow else rng.randint(2, 6)
            recent = rng.randrange(31, DAYS)
            offsets = sorted([recent] + rng.sample([d for d in range(1, DAYS) if d != recent], count-1))
            issues = [{'day': START+timedelta(days=d), 'quantity': rng.choices([1, 2, 3], [5, 3, 1])[0] if frequent else rng.choices([1, 2], [8, 1])[0]} for d in offsets]
            initial = balances.get(key)
            if initial is not None:
                addition = 0  # Existing initialized balances keep their original quantity.
            else:
                safety = part.safety_stock
                profile = rng.random()
                addition = 0 if profile < .18 else max(0, safety-1) if profile < .45 else rng.randint(max(1, safety), max(2, safety*2+1)) if profile < .85 else safety*3+rng.randint(1, 3)
            count_batches = rng.randint(1, min(3, len(issues)))
            boundaries = [0] + sorted(rng.sample(range(1, len(issues)), count_batches-1)) + [len(issues)]
            batches = []
            for batch_index, (left, right) in enumerate(zip(boundaries, boundaries[1:])):
                group = issues[left:right]
                quantity = sum(row['quantity'] for row in group) + (addition if batch_index == count_batches-1 else 0)
                supplier = rng.choice(eligible)
                receive_day = max(START, group[0]['day']-timedelta(days=rng.randint(2, 7)))
                ordered_day = receive_day-timedelta(days=supplier.lead_time_days)
                low, high = price_range(part.name)
                base = part.reference_price if part.reference_price else Decimal(rng.randint(low*100, high*100))/100
                price = (base*Decimal(rng.randint(94, 106))/100).quantize(Decimal('.01'))
                receipts = [{'day': receive_day, 'quantity': quantity}]
                if quantity > 1 and (group[0]['day']-receive_day).days >= 3 and rng.random() < .4:
                    first = rng.randint(1, quantity-1)
                    receipts = [{'day': receive_day, 'quantity': first}, {'day': receive_day+timedelta(days=rng.randint(1, 2)), 'quantity': quantity-first}]
                batches.append({'supplier_id': supplier.id, 'supplier_code': supplier.code, 'ordered_day': ordered_day,
                    'quantity': quantity, 'unit_price': price, 'receipts': receipts})
            plan.append({'part_id': part.id, 'sku': part.sku, 'name': part.name, 'warehouse_id': warehouse.id,
                'warehouse_code': warehouse.code, 'initial_quantity': initial, 'closing_quantity': initial if initial is not None else addition,
                'issues': issues, 'batches': batches})
    return plan


def import_history(session, operator_id, parts, warehouses, suppliers):
    """Caller commits once. Only missing warehouse/part issue histories are filled."""
    if END > date.today():
        raise ValueError('The fixed simulation period contains future dates')
    lock_catalogs(session, [w.id for w in warehouses], [p.id for p in parts], [s.id for s in suppliers])
    ids, warehouse_ids = [p.id for p in parts], [w.id for w in warehouses]
    issued = set(session.execute(select(Document.warehouse_id, DocumentLine.part_id).join(DocumentLine, DocumentLine.document_id == Document.id)
        .where(Document.kind == 'issue', Document.status == 'posted', DocumentLine.part_id.in_(ids), Document.warehouse_id.in_(warehouse_ids)).distinct()).all())
    balances = {(w, p): q for w, p, q in session.execute(select(StockBalance.warehouse_id, StockBalance.part_id, StockBalance.quantity)
        .where(StockBalance.part_id.in_(ids), StockBalance.warehouse_id.in_(warehouse_ids)))}
    plan = build_plan(parts, warehouses, suppliers, issued, balances)
    def request_id(name):
        return uuid5(NAMESPACE_URL, 'fours-catalog-random-v1:'+name)
    for row in plan:
        prefix = row['sku']+':'+row['warehouse_code']
        for batch_no, batch in enumerate(row['batches']):
            name = prefix+':purchase:'+str(batch_no)
            items = [{'part_id': row['part_id'], 'supplier_id': batch['supplier_id'], 'quantity': batch['quantity'], 'unit_price': batch['unit_price']}]
            old = existing_simulated_document(session, request_id(name), operator_id, 'purchase',
                row['warehouse_id'], batch['ordered_day'], items)
            if old is None:
                quote = register_simulated_prices(session, operator_id, row['part_id'], batch['supplier_id'], batch['unit_price'])
                batch['unit_price'] = items[0]['unit_price'] = quote.supply_price
                old = create_document(session, DocumentInput(request_id=request_id(name), kind='purchase', warehouse_id=row['warehouse_id'],
                    business_date=batch['ordered_day'], note=f'{NOTE}：{name}；模拟单价非真实报价', lines=items), operator_id)['id']
            purchase = {'id': old}
            review_simulated_document(session, purchase['id'], operator_id)
            transition(session, purchase['id'], 'confirm', ActionInput(request_id=request_id(name+':confirm'), business_date=batch['ordered_day']), operator_id)
            line = session.scalar(select(DocumentLine).where(DocumentLine.document_id == purchase['id']))
            for receipt_no, receipt in enumerate(batch['receipts']):
                receive_purchase(session, purchase['id'], ReceiptInput(request_id=request_id(name+':receipt:'+str(receipt_no)),
                    business_date=receipt['day'], note=f'{NOTE}：{name}：实际入库{receipt_no+1}',
                    lines=[{'purchase_line_id': line.id, 'quantity': receipt['quantity']}]), operator_id)
        for issue_no, issue in enumerate(row['issues']):
            name = prefix+':issue:'+str(issue_no)
            items = [{'part_id': row['part_id'], 'quantity': issue['quantity']}]
            old = existing_simulated_document(session, request_id(name), operator_id, 'issue', row['warehouse_id'], issue['day'], items)
            if old is None:
                register_simulated_prices(session, operator_id, row['part_id'])
                old = create_document(session, DocumentInput(request_id=request_id(name), kind='issue', warehouse_id=row['warehouse_id'],
                    business_date=issue['day'], note=f'{NOTE}：{name}；模拟维修领用', issue_type='paid_repair', lines=items), operator_id)['id']
            document = {'id': old}
            transition(session, document['id'], 'post', ActionInput(request_id=request_id(name+':post'), business_date=issue['day']), operator_id)
        actual = session.scalar(select(StockBalance.quantity).where(StockBalance.warehouse_id == row['warehouse_id'], StockBalance.part_id == row['part_id']))
        if actual != row['closing_quantity']:
            raise RuntimeError('Simulation balance mismatch; roll back the complete import')
    return plan


def main():
    settings = Settings()
    if settings.mysql_database != 'fours_agent_dev' or settings.mysql_user != 'fours_dev':
        raise RuntimeError('Restricted to fours_agent_dev / fours_dev')
    source = json.loads((ROOT/'data/catalog/parts-expansion.json').read_text(encoding='utf-8'))
    skus = [f'DEMO-{i:03}' for i in range(1, 13)] + [r['sku'] for r in source['parts']]
    engine = make_engine(settings)
    try:
        with Session(engine) as session, session.begin():
            operator = session.scalar(select(User).where(User.username == 'demo_admin', User.role == 'admin', User.active == True))
            parts = list(session.scalars(select(Part).where(Part.sku.in_(skus), Part.active == True).order_by(Part.sku)))
            warehouses = list(session.scalars(select(Warehouse).where(Warehouse.code.in_(['DEMO-W01', 'DEMO-W02']), Warehouse.active == True).order_by(Warehouse.code)))
            suppliers = list(session.scalars(select(Supplier).where(Supplier.active == True).order_by(Supplier.code)))
            if operator is None or len(parts) != 48 or len(warehouses) != 2:
                raise RuntimeError('Expected the existing 48 registered parts, two warehouses and active project administrator')
            before = {(w, p): q for w, p, q in session.execute(select(StockBalance.warehouse_id, StockBalance.part_id, StockBalance.quantity))}
            plan = import_history(session, operator.id, parts, warehouses, suppliers)
            after = {(w, p): q for w, p, q in session.execute(select(StockBalance.warehouse_id, StockBalance.part_id, StockBalance.quantity))}
            if any(after.get(key) != value for key, value in before.items()):
                raise RuntimeError('Existing balances changed; roll back the import')
            coverage = session.scalar(select(func.count(func.distinct(DocumentLine.part_id))).select_from(DocumentLine).join(Document, Document.id == DocumentLine.document_id)
                .where(Document.kind == 'issue', Document.status == 'posted', DocumentLine.part_id.in_([p.id for p in parts])))
            if coverage != 48:
                raise RuntimeError('Not all registered parts have issue history; roll back the import')
            report = {'source': 'catalog_random_simulation_v1', 'seed': SEED, 'consumption_from': str(START), 'consumption_to': str(END),
                'declared_days': DAYS, 'coverage_note': 'Calendar completeness applies only to newly generated pairs; unscheduled days are simulated zero, not measured history. Existing histories retain their original declared coverage.',
                'new_pairs': len(plan), 'new_parts': len({r['part_id'] for r in plan}), 'covered_parts': coverage,
                'existing_balances_preserved': True, 'catalog_prices_changed': False, 'plan': plan}
            if plan:
                path = ROOT/'data/catalog/random-history-manifest.json'
                if path.exists():
                    raise RuntimeError('Existing manifest must not be overwritten by a different import')
                pending = ROOT/'data/local-backups/catalog-history-manifest-pending.json'
                pending.parent.mkdir(parents=True, exist_ok=True)
                pending.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str)+'\n', encoding='utf-8')
        if plan:
            pending.replace(path)
        print(json.dumps({k: v for k, v in report.items() if k != 'plan'}, ensure_ascii=False))
    finally:
        engine.dispose()


if __name__ == '__main__':
    main()
