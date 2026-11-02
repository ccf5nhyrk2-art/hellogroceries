"""Offline-only baseline evaluation of ingredient quantity estimation.

This does not modify recipes, pantry inventory, or shopping lists. It estimates
from other recipes with the same normalized ingredient and measurement unit.
"""
from collections import defaultdict
from statistics import median
import math
from recipe_quality import parse_source_line, canonical_source_name, SHOPPING_UNITS, FRACTIONS


def number_value(raw):
    if raw in FRACTIONS:
        return FRACTIONS[raw]
    try:
        parts = raw.split()
        if len(parts) == 2 and "/" in parts[1]:
            a, b = parts[1].split("/")
            return float(parts[0]) + float(a) / float(b)
        if "/" in raw:
            a, b = raw.split("/")
            return float(a) / float(b)
        return float(raw)
    except (ValueError, ZeroDivisionError, TypeError):
        return None


def observations(recipe):
    lines = recipe.get("ingredient_text") or []
    if not isinstance(lines, list):
        return []
    result = []
    for line in lines:
        parsed = parse_source_line(line)
        if not parsed:
            continue
        name = canonical_source_name(parsed["ingredient"])
        unit = SHOPPING_UNITS.get(parsed["unit"].lower())
        amount = number_value(parsed["amount"])
        if name and unit and amount is not None and math.isfinite(amount) and 0 < amount < 100000:
            result.append((name, unit, amount))
    return result


def evaluate(recipes, limit=200, minimum_examples=3):
    """Hold out entire recipes; report coverage and errors on known amounts.

    No serving normalization is attempted because serving metadata can be
    inconsistent. This is a conservative baseline, not a production model.
    """
    ordered = sorted((r for r in recipes if isinstance(r, dict)), key=lambda r: str(r.get("id", "")))
    training = defaultdict(list)
    heldout = []
    for i, recipe in enumerate(ordered):
        rows = observations(recipe)
        if i % 5 == 0:
            heldout.append((recipe.get("id"), rows))
        else:
            for name, unit, amount in rows:
                training[(name, unit)].append(amount)
    checked = 0
    eligible = 0
    within_10 = within_25 = within_50 = 0
    errors = []
    examples = []
    for recipe_id, rows in heldout:
        for name, unit, actual in rows:
            if checked >= limit:
                break
            checked += 1
            samples = training.get((name, unit), [])
            if len(samples) < minimum_examples:
                continue
            estimate = median(samples)
            error = abs(estimate - actual) / actual
            eligible += 1
            errors.append(error)
            within_10 += error <= .10
            within_25 += error <= .25
            within_50 += error <= .50
            if len(examples) < 20:
                examples.append({"recipe_id": str(recipe_id), "ingredient": name,
                                 "unit": unit, "actual": actual, "estimated": estimate,
                                 "relative_error": round(error, 3), "training_examples": len(samples)})
        if checked >= limit:
            break
    return {"mode": "offline_validation_only", "evaluated": checked, "covered": eligible,
            "coverage": round(eligible / checked, 3) if checked else 0,
            "within_10_percent": round(within_10 / eligible, 3) if eligible else None,
            "within_25_percent": round(within_25 / eligible, 3) if eligible else None,
            "within_50_percent": round(within_50 / eligible, 3) if eligible else None,
            "median_relative_error": round(median(errors), 3) if errors else None,
            "examples": examples, "applied_changes": 0}
