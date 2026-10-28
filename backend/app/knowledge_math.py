"""Four hypothetical calculations. No database access, proposals or stock changes."""
import re
from datetime import date, timedelta
from fractions import Fraction
from math import ceil


class CalculationInputError(ValueError):
    pass


LABELS = {
    "issue_30d": ("领用", "消耗"), "days": ("天", "日"), "lead_days": ("交期", "交货"),
    "safety": ("安全",), "available": ("可用",), "purchase_pending": ("采购待收", "采购未收"),
    "transfer_inbound": ("调拨在途", "调入在途"), "source": ("来源仓", "源仓"),
    "target": ("目标",), "quantity": ("调拨",), "baseline": ("基准",), "gap": ("缺口",),
    "code": (),
}


def quoted_value(field, row, question):
    if field == 'code' and isinstance(row, str) and 1 <= len(row) <= 40 and re.search(r'(?<![A-Za-z0-9_-])'+re.escape(row)+r'(?![A-Za-z0-9_-])', question):
        return row
    if not isinstance(row, dict) or set(row) != {"value", "quote"}:
        raise CalculationInputError("每项输入均需数值和本轮原文出处")
    value, quote = row["value"], row["quote"]
    if not isinstance(quote, str) or not quote or len(quote) > 250 or quote not in question:
        raise CalculationInputError("输入出处必须逐字来自本轮问题，且不超过250字符")
    if field == 'gap' and re.fullmatch(r'各\s*\d+\s*件', quote) and question.count(quote) == 1:
        # A shared quantity inherits its explicit field label in the same clause.
        prefix = re.split(r'[；;。\n]', question[:question.index(quote)])[-1]
        if '缺口' in prefix and len(prefix + quote) <= 250:
            quote = prefix + quote
    warehouse_alias = False
    if field in {'source', 'target'}:
        direction = re.search(r'([A-Za-z0-9]+)(?:仓)?.{0,2}(?:向|到|至)([A-Za-z0-9]+)(?:仓)?.{0,8}调拨', question)
        if direction:
            code = direction.group(1 if field == 'source' else 2)
            warehouse_alias = re.search(r'(?<![A-Za-z0-9])'+re.escape(code)+r'(?:仓)?', quote) is not None
    if not any(label in quote for label in LABELS[field]) and LABELS[field] and not warehouse_alias:
        raise CalculationInputError("输入出处缺少字段含义：" + field)
    if field in {"baseline", "code"}:
        if not isinstance(value, str) or not value or value not in quote:
            raise CalculationInputError("日期或编码与原文不符")
    elif (type(value) is not int or not 0 <= value <= 10**9
            or not re.search(r"(?<![\d.\-])" + str(value) + r"(?![\d.])", quote)
            or not (any(unit in quote for unit in (("天", "日") if field in {"days", "lead_days"} else ("件",)))
                or (field in {'available','target','gap'} and '同单位' in question and '件' in question
                    and not re.search(r'\d+\s*(?:升|桶|瓶|套)', question)))):
        raise CalculationInputError("数量需为带件数或天数单位的非负整数，且与原文一致")
    if type(value) is int and field not in {'days'}:
        labels = [code+r'(?:仓)?'] if warehouse_alias else [re.escape(label) for label in LABELS[field]]
        if not any(re.search(label+r'[^\d；;。\n]{0,10}'+str(value)+r'(?![\d.])',quote) for label in labels):
            raise CalculationInputError('输入数值与对应字段未明确关联：'+field)
    return value


def calculate_example(spec, question):
    if isinstance(spec, dict) and spec.get('kind') == 'allocation' and 'inputs' not in spec and set(spec) == {'kind','hypothesis_quote','citation_ids','sources','demands','order_quote'}:
        spec = {k:v for k,v in spec.items() if k in {'kind','hypothesis_quote','citation_ids'}} | {'inputs':{k:spec[k] for k in ('sources','demands','order_quote')}}
    if not isinstance(spec, dict) or set(spec) != {"kind", "hypothesis_quote", "inputs", "citation_ids"}:
        raise CalculationInputError("计算类型、假设声明、输入或依据缺失")
    quote = spec["hypothesis_quote"]
    if not isinstance(quote, str) or quote not in question or not any(w in quote for w in ("假设", "假定", "示例", "举例")):
        raise CalculationInputError("请明确这是使用所给参数的假设计算")
    kind, inputs = spec["kind"], spec["inputs"]
    keys = {
        "replenishment": {"issue_30d", "days", "lead_days", "safety", "available", "purchase_pending", "transfer_inbound"},
        "transfer_conservation": {"source", "target", "quantity"},
        "complete_day_window": {"baseline", "days"},
        "allocation": {"sources", "demands", "order_quote"},
    }
    if kind not in keys or not isinstance(inputs, dict) or set(inputs) != keys[kind]:
        raise CalculationInputError("计算条件不完整或存在多余条件")
    if kind == "allocation":
        order_quote = inputs["order_quote"]
        if order_quote == '' and re.search(r'(?:优先|先)(?:从|给|满足|使用)?[A-Za-z0-9]+仓|按.{0,16}(?:编码降序|余量升序|距离|成本|先到)', question):
            raise CalculationInputError('问题指定了分配顺序，请明确是否采用制度默认顺序，不能静默覆盖')
        default_declared = isinstance(order_quote,str) and order_quote in question and re.search(r'制度默认|默认规则|默认顺序',order_quote)
        if (not isinstance(order_quote, str) or (order_quote and not default_declared and (order_quote not in question or not all(w in order_quote for w in ("余量降序", "编码升序", "需求顺序"))))):
            raise CalculationInputError("请明确按可调余量降序、同余量编码升序、需求顺序分配")
        values = {}
        for group, required in (("sources", {"code", "available", "target"}), ("demands", {"code", "gap"})):
            rows = inputs[group]
            if not isinstance(rows, list) or not 1 <= len(rows) <= 20 or any(not isinstance(r, dict) or set(r) != required for r in rows):
                raise CalculationInputError("每组仓库需有完整条件且限1至20项")
            values[group] = [{k: quoted_value(k, v, question) for k, v in r.items()} for r in rows]
            for raw, row in zip(rows,values[group]):
                for key in required-{'code'}:
                    quote = raw[key]['quote']; start = question.find(quote)
                    section = re.split(r'[；;。\n]',question[:start])[-1] + quote if start >= 0 else ''
                    if row['code'] not in section:
                        raise CalculationInputError("每行数量出处还需明确对应仓库编码")
            if len({r["code"] for r in values[group]}) != len(rows):
                raise CalculationInputError("仓库编码不能重复")
        if {r["code"] for r in values["sources"]} & {r["code"] for r in values["demands"]}:
            raise CalculationInputError("本例来源与目标仓不能重叠")
        remaining = {r["code"]: max(0, r["available"] - r["target"]) for r in values["sources"]}
        order = sorted(remaining, key=lambda c: (-remaining[c], c))
        allocations, gaps = [], {}
        for demand in values["demands"]:
            gap = demand["gap"]
            for code in order:
                quantity = min(gap, remaining[code])
                if quantity:
                    allocations.append({"source": code, "target": demand["code"], "quantity": quantity})
                    remaining[code] -= quantity
                    gap -= quantity
            gaps[demand["code"]] = gap
        result = {"allocations": allocations, "purchase_gaps": gaps}
        steps = ["可调量=max(0,可用量−目标量)", "按可调余量降序、同余量编码升序、所列需求顺序分配，每次扣减来源剩余可调量；剩余缺口作为采购缺口"]
    else:
        values = {k: quoted_value(k, v, question) for k, v in inputs.items()}
        if kind in {"replenishment", "complete_day_window"} and values["days"] != 30:
            raise CalculationInputError("当前口径为30个完整自然日，不能使用不足或其他天数的历史")
        if kind == "replenishment":
            history_quote = inputs['issue_30d']['quote']
            if not re.search(r'(?:30.{0,3}完整|完整.{0,3}30)', history_quote) or any(w in history_quote for w in ('未过账','不足')):
                raise CalculationInputError("请明确消耗来自30个完整自然日的已过账领用记录")
            daily = Fraction(values["issue_30d"], 30)
            target = ceil(daily * values["lead_days"] + values["safety"])
            position = sum(values[k] for k in ("available", "purchase_pending", "transfer_inbound"))
            result = {"daily_consumption": str(daily), "target": target, "position": position, "gap": max(0, target-position)}
            steps = [f"平均日消耗={values['issue_30d']}/30={daily}件/天", f"目标库存=ceil({daily}×{values['lead_days']}+{values['safety']})={target}件",
                f"库存位置={values['available']}+{values['purchase_pending']}+{values['transfer_inbound']}={position}件", f"补货缺口=max(0,{target}−{position})={result['gap']}件"]
            if '已过账' not in history_quote:
                steps.insert(0, '本例假定所给完整30日领用量采用已过账口径；未核验业务记录，不用于实际补货执行。')
        elif kind == "transfer_conservation":
            source, target, quantity = (values[k] for k in ("source", "target", "quantity"))
            if not 0 < quantity <= source:
                raise CalculationInputError("调拨数量须为正整数且不超过来源仓数量")
            result = {"after_ship": {"source": source-quantity, "target": target, "in_transit": quantity},
                "after_receive": {"source": source-quantity, "target": target+quantity, "in_transit": 0}, "total": source+target}
            steps = ["发出时来源仓扣减，目标仓现有量不增加，调拨量计入在途", f"收货时在途转为目标仓现有量；现有量加在途始终为{source+target}件"]
        else:
            try:
                baseline = date.fromisoformat(values["baseline"])
                start, end = baseline-timedelta(days=30), baseline-timedelta(days=1)
            except (ValueError, OverflowError):
                raise CalculationInputError("基准日期无效或超出范围") from None
            result = {"start": str(start), "end": str(end), "count": 30}
            steps = [f"从基准日{baseline}向前取30个完整自然日，不含基准日", f"起日{start}，止日{end}，共30日"]
    return {"kind": kind, "inputs": values, "input_evidence": inputs, "steps": steps, "result": result,
        "citation_ids": spec["citation_ids"], "notice": "按你提供的假设计算；未核实业务事实，未查询实时库存或办理单据。"}
