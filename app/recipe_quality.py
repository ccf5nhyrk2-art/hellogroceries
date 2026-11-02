"""Conservative, source-aware ingredient quality checks.

Never infer an ingredient amount from servings or a generic recipe title.
A numeric ingredient multiplier is not evidence of a physical unit.
"""
import math
import re
from functools import lru_cache

NUMBER = r"(?:\d+(?:\.\d+)?|\d+/\d+|[¼½¾⅓⅔⅛⅜⅝⅞])"
UNITS = r"(?:teaspoons?|tsp|tablespoons?|tbsp|cups?|ounces?|oz|pounds?|lbs?|grams?|g|kilograms?|kg|milliliters?|ml|liters?|l|cloves?|bulbs?|fillets?|pieces?|bunch(?:es)?|heads?|stalks?|slices?|cans?|packages?|bags?)"
EXPLICIT = re.compile(r"^\s*("+NUMBER+r")\s+(?:(("+UNITS+r")\s+))?(.+?)\s*$", re.I)
MEASURED = re.compile(r"^\s*("+NUMBER+r")\s+("+UNITS+r")\s+(.+?)\s*$", re.I)
UNIT_ONLY = re.compile(r"^\s*(?:"+UNITS+r")(?:\s*\([^)]*\))?\s+.+$", re.I)
SOURCE_UNIT = re.compile(r"^\s*(?:"+UNITS+r")(?:\s*\([^)]*\))?\s+", re.I)

def canonical_source_name(name):
    if not isinstance(name, str):
        return ""
    name = SOURCE_UNIT.sub("", name.strip())
    name = re.sub(r"[^a-z0-9]+", " ", name.casefold()).strip()
    return name

AMBIGUOUS = re.compile(r"\b(?:garlic|grape tomatoes?|jalape(?:ñ|n)o(?: peppers?)?|salmon|pepper|salt|cilantro|parsley|sour cream|bell peppers?)\b", re.I)


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
    if UNIT_ONLY.match(name):
        return {"status": "unit_labeled", "reason": "Unit identified but numeric quantity must be verified", "source": name}
    return {"status": "needs_source", "reason": "Source label has no explicit measurement; multiplier is not a unit", "source": name}


def _inspect_with_source(recipe, name, factor):
    result = inspect_ingredient(name, factor)
    if result["status"] in ("needs_source", "unit_labeled"):
        recovered = recover_from_stored_source(recipe, name)
        if recovered:
            return {"status": "source_recovered", "reason": "Verified in stored original ingredient text", "recovery": recovered}
    return result


def audit_recipe(recipe):
    ingredients = recipe.get("ingredients") if isinstance(recipe, dict) else None
    if not isinstance(ingredients, dict):
        return []
    return [
        {"recipe_id": str(recipe.get("id", "")), "ingredient": name,
         **_inspect_with_source(recipe, name, factor)}
        for name, factor in ingredients.items()
    ]


def audit_catalog(recipes, limit=200):
    counts = {"recipes": 0, "ingredients": 0, "verified": 0, "verified_count": 0, "source_recovered": 0, "unit_labeled": 0, "needs_source": 0, "invalid": 0}
    issues = []
    for recipe in recipes:
        counts["recipes"] += 1
        for item in audit_recipe(recipe):
            counts["ingredients"] += 1
            counts[item["status"]] += 1
            if item["status"] in ("needs_source", "unit_labeled", "invalid") and len(issues) < limit:
                issues.append(item)
    return {"counts": counts, "issues": issues, "complete": True, "estimates_applied": 0}

def recover_from_stored_source(recipe, ingredient):
    """Recover only when a single unambiguous stored source line names the ingredient."""
    if not isinstance(recipe, dict) or not isinstance(ingredient, str):
        return None
    lines = recipe.get("ingredient_text")
    if not isinstance(lines, list):
        return None
    target = canonical_source_name(ingredient)
    if not target:
        return None
    matches = []
    for line in lines:
        if not isinstance(line, str):
            continue
        measured = MEASURED.match(line)
        if not measured:
            continue
        name = canonical_source_name(measured.group(3))
        if name == target:
            matches.append({"source": line, "amount": measured.group(1),
                            "unit": measured.group(2), "ingredient": measured.group(3),
                            "confidence": "verified_source"})
    return matches[0] if len(matches) == 1 else None

FRACTIONS = {"¼": .25, "½": .5, "¾": .75, "⅓": 1/3, "⅔": 2/3,
             "⅛": .125, "⅜": .375, "⅝": .625, "⅞": .875}
SHOPPING_UNITS = {
    "tsp": "teaspoon", "teaspoons": "teaspoon", "teaspoon": "teaspoon",
    "tbsp": "tablespoon", "tablespoons": "tablespoon", "tablespoon": "tablespoon",
    "cups": "cup", "cup": "cup", "oz": "ounce", "ounces": "ounce", "ounce": "ounce",
    "lbs": "pound", "pounds": "pound", "pound": "pound",
    "grams": "gram", "gram": "gram", "g": "gram",
    "kilograms": "kilogram", "kilogram": "kilogram", "kg": "kilogram",
    "milliliters": "milliliter", "milliliter": "milliliter", "ml": "milliliter",
    "liters": "liter", "liter": "liter", "l": "liter",
    "cloves": "clove", "clove": "clove",
    "fillets": "fillet", "fillet": "fillet",
    "pieces": "piece", "piece": "piece"
}

def verified_grocery_recovery(recipe, ingredient, factor):
    """Safe auto-apply candidate; leave unsupported units and uncertain factors alone."""
    try:
        if float(factor) != 1.0:
            return None
    except (TypeError, ValueError, OverflowError):
        return None
    recovered = recover_from_stored_source(recipe, ingredient)
    if not recovered:
        return None
    raw = recovered["amount"]
    try:
        amount = FRACTIONS[raw] if raw in FRACTIONS else (
            float(raw.split("/")[0]) / float(raw.split("/")[1]) if "/" in raw else float(raw))
    except (ValueError, ZeroDivisionError):
        return None
    unit = SHOPPING_UNITS.get(recovered["unit"].lower())
    if not unit or not math.isfinite(amount) or not 0 < amount < 100000:
        return None
    return {"name": recovered["ingredient"], "unit": unit, "amount": amount,
            "source": recovered["source"], "confidence": "verified_source"}
