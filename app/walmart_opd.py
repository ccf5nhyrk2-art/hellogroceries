"""Walmart OPD staging-only integration boundary.

No network calls are made until Walmart explicitly approves OPD access.
Do not import credentials into client-side code.
"""
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class WalmartCartItem:
    ingredient: str
    quantity: float
    unit: str
    product_id: Optional[str] = None


class WalmartAccessPending(RuntimeError):
    pass


def prepare_items(shopping_items):
    """Normalize already-calculated shopping list items; never recompute pantry deductions."""
    result = []
    for item in shopping_items:
        name = str(item.get("name", "")).strip()
        amount = item.get("quantity", 0)
        unit = str(item.get("unit", "")).strip()
        if not name or isinstance(amount, bool):
            continue
        try:
            quantity = float(amount)
        except (TypeError, ValueError):
            continue
        if quantity <= 0:
            continue
        result.append(WalmartCartItem(name, quantity, unit))
    return result


def add_to_cart(*_args, **_kwargs):
    """Fail closed until OPD approval and an authenticated adapter are implemented."""
    raise WalmartAccessPending("Walmart cart transfer is awaiting OPD partner approval.")
