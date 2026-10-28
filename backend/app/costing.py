"""Deterministic inventory value allocation; never use a current quote as cost."""
from decimal import Decimal, ROUND_HALF_UP, localcontext


def money(value):
    with localcontext() as context:
        context.prec = 40
        return Decimal(value).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)


def allocated_cost(value, quantity, out_quantity):
    if quantity <= 0 or out_quantity <= 0 or out_quantity > quantity:
        raise ValueError('Invalid stock allocation quantity')
    if value is None:
        return None
    if not value.is_finite() or value < 0:
        raise ValueError('Invalid inventory value')
    if out_quantity == quantity:
        return money(value)
    with localcontext() as context:
        context.prec = 40
        return money(value * out_quantity / quantity)
