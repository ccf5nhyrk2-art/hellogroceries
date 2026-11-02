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


def serving_count(recipe):
    """Only trust explicit, bounded serving metadata."""
    raw = recipe.get("source_servings")
    if isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and 0 < value <= 20 else None


def category(name):
    name = name.casefold()
    if any(word in name for word in ("salt", "pepper", "spice", "seasoning", "paprika", "cumin")):
        return "seasoning"
    if any(word in name for word in ("chicken", "beef", "pork", "fish", "salmon", "shrimp", "turkey")):
        return "protein"
    if any(word in name for word in ("rice", "pasta", "noodle", "couscous", "quinoa", "bread")):
        return "grain_or_starch"
    if any(word in name for word in ("mayonnaise", "mustard", "ketchup", "sauce", "dressing", "salsa", "pesto")):
        return "sauces_condiments"
    if any(word in name for word in ("butter", "oil", "ghee")):
        return "oils_fats"
    if any(word in name for word in ("cream", "cheese", "milk", "yogurt")):
        return "dairy"
    return "other"


def evaluate(recipes, limit=1500, minimum_examples=5):
    """Recipe-level holdout with per-serving normalization and cautious abstention.

    Reports overall prediction error and separately the coverage of high-consensus
    suggestions. Does not alter source records or make shopping-list predictions.
    """
    ordered = sorted((r for r in recipes if isinstance(r, dict)),
                     key=lambda r: str(r.get("id", "")))
    training = defaultdict(list)
    heldout = []
    for i, recipe in enumerate(ordered):
        servings = serving_count(recipe)
        rows = observations(recipe)
        if i % 5 == 0:
            heldout.append((recipe.get("id"), servings, rows))
        else:
            for name, unit, amount in rows:
                # Never mix serving-normalized and unnormalized training examples.
                key = (name, unit, "per_serving" if servings else "raw")
                training[key].append(amount / servings if servings else amount)
    checked = covered = within_10 = within_25 = within_50 = 0
    errors = []
    examples = []
    categories = defaultdict(lambda: {"tested": 0, "covered": 0, "within_25": 0, "high_consensus": 0, "high_consensus_within_25": 0})
    high_confidence = {"eligible": 0, "within_25": 0, "within_10": 0}
    for recipe_id, servings, rows in heldout:
        for name, unit, actual in rows:
            if checked >= limit:
                break
            checked += 1
            group = categories[category(name)]
            group["tested"] += 1
            key = (name, unit, "per_serving" if servings else "raw")
            samples = training.get(key, [])
            if len(samples) < minimum_examples:
                continue
            predicted_unit = median(samples)
            estimate = predicted_unit * servings if servings else predicted_unit
            error = abs(estimate - actual) / actual
            covered += 1
            group["covered"] += 1
            errors.append(error)
            within_10 += error <= .10
            within_25 += error <= .25
            within_50 += error <= .50
            group["within_25"] += error <= .25
            spread = sorted(abs(x - predicted_unit) / predicted_unit for x in samples)
            variable = category(name) in ("sauces_condiments", "oils_fats", "dairy", "seasoning")
            consensus = len(samples) >= (20 if variable else 10) and median(spread) <= (.05 if variable else .10) and spread[int(.8 * (len(spread) - 1))] <= (.10 if variable else .25)
            if consensus:
                group["high_consensus"] += 1
                group["high_consensus_within_25"] += error <= .25
                high_confidence["eligible"] += 1
                high_confidence["within_25"] += error <= .25
                high_confidence["within_10"] += error <= .10
            if len(examples) < 25:
                examples.append({"recipe_id": str(recipe_id), "ingredient": name,
                                 "unit": unit, "actual": actual,
                                 "estimated": round(estimate, 4),
                                 "relative_error": round(error, 3),
                                 "training_examples": len(samples),
                                 "serving_normalized": bool(servings),
                                 "high_consensus": consensus})
        if checked >= limit:
            break
    return {"mode": "offline_validation_only", "evaluated": checked,
            "covered": covered, "coverage": round(covered / checked, 3) if checked else 0,
            "within_10_percent": round(within_10 / covered, 3) if covered else None,
            "within_25_percent": round(within_25 / covered, 3) if covered else None,
            "within_50_percent": round(within_50 / covered, 3) if covered else None,
            "median_relative_error": round(median(errors), 3) if errors else None,
            "high_confidence": {
                "eligible": high_confidence["eligible"],
                "within_10_percent": round(high_confidence["within_10"] / high_confidence["eligible"], 3) if high_confidence["eligible"] else None,
                "within_25_percent": round(high_confidence["within_25"] / high_confidence["eligible"], 3) if high_confidence["eligible"] else None},
            "categories": {key: {**value, "within_25_percent": round(value["within_25"] / value["covered"], 3) if value["covered"] else None, "high_consensus_within_25_percent": round(value["high_consensus_within_25"] / value["high_consensus"], 3) if value["high_consensus"] else None} for key, value in sorted(categories.items())},
            "examples": examples, "applied_changes": 0}
