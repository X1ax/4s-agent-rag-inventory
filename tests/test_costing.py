from decimal import Decimal

import pytest


def calculator():
    try:
        from backend.app.costing import allocated_cost, money
    except ImportError:
        pytest.fail('Actual-cost allocation is not implemented')
    return allocated_cost, money


def test_weighted_allocation_uses_total_value_and_rounds_half_up():
    allocate, money = calculator()
    assert allocate(Decimal('2200.00'), 20, 3) == Decimal('330.00')
    assert allocate(Decimal('0.05'), 2, 1) == Decimal('0.03')
    assert money(Decimal('1.005')) == Decimal('1.01')


def test_last_piece_consumes_full_residual_value_and_unknown_cost_stays_unknown():
    allocate, _ = calculator()
    assert allocate(Decimal('0.02'), 1, 1) == Decimal('0.02')
    assert allocate(None, 5, 2) is None
    assert allocate(Decimal('0.00'), 5, 2) == Decimal('0.00')


def test_allocation_rejects_invalid_or_oversold_quantities():
    allocate, _ = calculator()
    for value, quantity, out in [('1', 0, 1), ('1', 2, 3), ('1', 2, -1), ('-1', 2, 1)]:
        with pytest.raises(ValueError):
            allocate(Decimal(value), quantity, out)


def test_large_stock_value_preserves_cents():
    allocate, _ = calculator()
    assert allocate(Decimal('9999999999990000000.00'), 1000000000, 1000000000) == Decimal('9999999999990000000.00')
