"""Conservative, source-aware ingredient quality checks.

Never infer an ingredient amount from servings or a generic recipe title.
A numeric ingredient multiplier is not evidence of a physical unit.
"""
import math
import re

NUMBER = r"(?:\\d+(?:\\.\\d+)?|\\d+/\\d+|[¼½¾⅓⅔⅛⅜⅝⅞])"
UNITS = r"(?:teaspoons?|tsp|tablespoons?|tbsp|cups?|ounces?|oz|pounds?|lbs?|grams?|g|kilograms?|kg|milliliters?|ml|liters?|l|cloves?|bulbs?|fillets?|pieces?|bunch(?:es)?|heads?|stalks?|slices?|cans?|packages?|bags?)"
EXPLICIT = re.compile(r"^\\s*("+NUMBER+r")\\s+(?:(("+UNITS+r")\\s+))?(.+?)\\s*$", re.I)
MEASURED = re.compile(r"^\\s*("+NUMBER+r")\\s+("+UNITS+r")\\s+(.+?)\\s*$", re.I)
AMBIGUOUS = re.compile(r"\\b(?:garlic|grape tomatoes?|jalape(?:ñ|n)o(?: peppers?)?|salmon|pepper|salt|cilantro|parsley|sour cream|bell peppers?)\\b", re.I)


def inspect_ingredient(name, factor):
    if not isinstance(name, str) or not name.strip():
        return {"status": "invalid", "reason": "Empty ingredient label"}
    try:
        value = float(factor)
    except (TypeError, ValueError, OverflowError):
        return {"status": "invalid", "reason": "Invalid source multiplier"}
    if not math.isfinite(value) or value <= 0:
        return {"status": "invalid", "reason": "Nonpositive source multiplier"}
    match = MEASURED.match(name)
    if match:
        return {"status": "verified", "reason": "Explicit amount and unit in source label", "source": name}
    match = EXPLICIT.match(name)
    if match:
        return {"status": "verified_count", "reason": "Explicit count in source label", "source": name}
    return {"status": "needs_source", "reason": "Source label has no explicit measurement; multiplier is not a unit", "source": name}


def audit_recipe(recipe):
    ingredients = recipe.get("ingredients") if isinstance(recipe, dict) else None
    if not isinstance(ingredients, dict):
        return []
    return [
        {"recipe_id": str(recipe.get("id", "")), "ingredient": name, **inspect_ingredient(name, factor)}
        for name, factor in ingredients.items()
    ]


def audit_catalog(recipes, limit=200):
    counts = {"recipes": 0, "ingredients": 0, "verified": 0, "verified_count": 0, "needs_source": 0, "invalid": 0}
    issues = []
    for recipe in recipes:
        counts["recipes"] += 1
        for item in audit_recipe(recipe):
            counts["ingredients"] += 1
            counts[item["status"]] += 1
            if item["status"] in ("needs_source", "invalid") and len(issues) < limit:
                issues.append(item)
    return {"counts": counts, "issues": issues, "complete": True, "estimates_applied": 0}
