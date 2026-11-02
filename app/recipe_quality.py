"""Conservative, source-aware ingredient quality checks.

Never infer an ingredient amount from servings or a generic recipe title.
A numeric ingredient multiplier is not evidence of a physical unit.
"""
import math
import re
from functools import lru_cache

NUMBER = r"(?:\d+\s+\d+/\d+|\d+(?:\.\d+)?|\d+/\d+|[¼½¾⅓⅔⅛⅜⅝⅞])"
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
    # Normalize only predictable inflection; do not conflate varieties,
    # colors or qualifiers (e.g. herb butter and butter).
    words = name.split()
    if words:
        last = words[-1]
        if last.endswith("ies") and len(last) > 4:
            words[-1] = last[:-3] + "y"
        elif last.endswith("oes") and len(last) > 4:
            words[-1] = last[:-2]
        elif last.endswith("s") and not last.endswith(("ss", "us")) and len(last) > 3:
            words[-1] = last[:-1]
    return " ".join(words)

# Only accept a single, explicit quantity. Ranges and package-size expressions
# are deliberately excluded until their semantics can be verified.
TO_TASTE = re.compile(r"\b(?:to taste|as needed|for serving|optional)\b", re.I)
RANGE = re.compile(r"^\s*(?:\d+(?:\.\d+)?\s*(?:-|–|—|to)\s*\d+|\d+\s*\([^)]*\))", re.I)
SOURCE_MEASURE = re.compile(r"^\s*("+NUMBER+r")\s*("+UNITS+r")\.?\s*(?:of\s+)?(.+?)\s*$", re.I)

def parse_source_line(line):
    if not isinstance(line, str) or TO_TASTE.search(line) or RANGE.match(line):
        return None
    normalized = line.strip().replace("⁄", "/")
    normalized = re.sub(r"^(\d+(?:/\d+)?)\s*(?:\(\s*(?:\d+\s*)?(?:oz|ounces?|g|grams?|ml|milliliters?)\s*\)\s*)+(?:cans?|packages?|bags?)\b", r"\1 package", normalized, flags=re.I)
    matched = SOURCE_MEASURE.match(normalized)
    if not matched:
        return None
    return {"amount": matched.group(1), "unit": matched.group(2), "ingredient": matched.group(3), "source": line}

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
                issues.append({**item, "source_diagnostic": source_match_diagnostic(recipe, item["ingredient"])})
    return {"counts": counts, "issues": issues, "complete": True, "estimates_applied": 0}

def source_match_diagnostic(recipe, ingredient):
    if not isinstance(recipe, dict):
        return {"status": "invalid_recipe"}
    lines = recipe.get("ingredient_text")
    if not isinstance(lines, list) or not lines:
        return {"status": "no_source_lines"}
    target = canonical_source_name(ingredient)
    matches = []
    related = []
    for line in lines:
        if not isinstance(line, str):
            continue
        parsed = parse_source_line(line)
        if parsed and canonical_source_name(parsed["ingredient"]) == target:
            matches.append(parsed)
        elif target and target in canonical_source_name(line):
            related.append(line)
    if matches:
        unique = {(m["amount"], SHOPPING_UNITS.get(m["unit"].lower())) for m in matches}
        if len(unique) > 1:
            return {"status": "conflicting_quantities", "candidate_count": len(unique)}
        if not all(SHOPPING_UNITS.get(m["unit"].lower()) for m in matches):
            return {"status": "unsupported_unit"}
        return {"status": "matched", "candidate_count": len(matches)}
    if any(TO_TASTE.search(line) for line in related):
        return {"status": "to_taste_no_amount"}
    if any(RANGE.match(line) for line in related):
        return {"status": "range_requires_review"}
    if any(UNIT_ONLY.match(line) for line in related):
        return {"status": "unit_without_quantity", "example": related[0][:120]}
    if related:
        return {"status": "source_without_verified_quantity", "example": related[0][:120]}
    return {"status": "ingredient_not_found"}


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
        measured = parse_source_line(line)
        if not measured:
            continue
        name = canonical_source_name(measured["ingredient"])
        if name == target:
            matches.append({**measured, "confidence": "verified_source"})
    # Duplicate identical source lines are not conflicting evidence.
    unique = {(m["amount"], m["unit"].casefold(), canonical_source_name(m["ingredient"])): m for m in matches}
    return next(iter(unique.values())) if len(unique) == 1 else None

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
        if raw in FRACTIONS:
            amount = FRACTIONS[raw]
        elif "/" in raw:
            parts = raw.split()
            if len(parts) == 2:
                amount = float(parts[0]) + float(parts[1].split("/")[0]) / float(parts[1].split("/")[1])
            else:
                numerator, denominator = raw.split("/")
                amount = float(numerator) / float(denominator)
        else:
            amount = float(raw)
    except (ValueError, ZeroDivisionError):
        return None
    unit = SHOPPING_UNITS.get(recovered["unit"].lower())
    if not unit or not math.isfinite(amount) or not 0 < amount < 100000:
        return None
    return {"name": recovered["ingredient"], "unit": unit, "amount": amount,
            "source": recovered["source"], "confidence": "verified_source"}
