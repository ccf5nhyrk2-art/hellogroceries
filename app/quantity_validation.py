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


def recipe_context(recipe):
    """Conservative title-based meal type; never infer quantities from the title."""
    title = str(recipe.get("title") or recipe.get("name") or "").casefold()
    matches = [word for word in ("salad", "soup", "pasta", "taco", "burger", "bowl")
               if word in title]
    return matches[0] if len(matches) == 1 else "unspecified"


def evaluate(recipes, limit=1500, minimum_examples=5):
    """Recipe-level holdout with per-serving normalization and cautious abstention.

    Reports overall prediction error and separately the coverage of high-consensus
    suggestions. Does not alter source records or make shopping-list predictions.
    """
    ordered = sorted((r for r in recipes if isinstance(r, dict)),
                     key=lambda r: str(r.get("id", "")))
    training = defaultdict(list)
    contextual_training = defaultdict(list)
    heldout = []
    for i, recipe in enumerate(ordered):
        servings = serving_count(recipe)
        rows = observations(recipe)
        # Count each recipe only once per ingredient/unit. Conflicting repeated
        # amounts are ambiguous and cannot safely support a consensus estimate.
        unique = {}
        ambiguous = set()
        for name, unit, amount in rows:
            pair = (name, unit)
            if pair in unique and not math.isclose(unique[pair], amount, rel_tol=1e-9):
                ambiguous.add(pair)
            else:
                unique[pair] = amount
        rows = [(name, unit, amount) for (name, unit), amount in unique.items()
                if (name, unit) not in ambiguous]
        if i % 5 == 0:
            heldout.append((recipe.get("id"), servings, recipe_context(recipe), rows))
        else:
            for name, unit, amount in rows:
                # Never mix serving-normalized and unnormalized training examples.
                key = (name, unit, "per_serving" if servings else "raw", recipe_context(recipe))
                training[(name, unit, "per_serving" if servings else "raw")].append(amount / servings if servings else amount)
                contextual_training[key].append(amount / servings if servings else amount)
    checked = covered = within_10 = within_25 = within_50 = 0
    errors = []
    examples = []
    categories = defaultdict(lambda: {"tested": 0, "covered": 0, "within_25": 0, "high_consensus": 0, "high_consensus_within_25": 0})
    high_confidence = {"eligible": 0, "within_25": 0, "within_10": 0}
    context_counts = {"contextual": 0, "fallback": 0}
    method_stats = {method: {"covered": 0, "within_10": 0, "within_25": 0, "high_consensus": 0, "high_consensus_within_25": 0} for method in ("contextual", "fallback")}
    for recipe_id, servings, context, rows in heldout:
        for name, unit, actual in rows:
            if checked >= limit:
                break
            checked += 1
            group = categories[category(name)]
            group["tested"] += 1
            key = (name, unit, "per_serving" if servings else "raw")
            contextual_samples = contextual_training.get((*key, context), [])
            use_context = context != "unspecified" and len(contextual_samples) >= minimum_examples
            samples = contextual_samples if use_context else training.get(key, [])
            if len(samples) < minimum_examples:
                continue
            # Avoid a context-specific model when its observed quantities are
            # substantially less stable than the broader ingredient baseline.
            # The choice uses training data only, never the held-out answer.
            if use_context:
                baseline = training.get(key, [])
                if len(baseline) >= minimum_examples:
                    def relative_mad(values):
                        center = median(values)
                        return median(abs(value - center) for value in values) / center if center > 0 else float("inf")
                    # Compare the contextual and fallback predictions on the
                    # *same* contextual training examples, leaving each example
                    # out before scoring. This avoids choosing context solely
                    # because its distribution happens to be narrow.
                    if len(contextual_samples) >= 8:
                        context_errors = []
                        baseline_errors = []
                        for value in contextual_samples:
                            context_rest = list(contextual_samples)
                            context_rest.remove(value)
                            baseline_rest = list(baseline)
                            baseline_rest.remove(value)
                            if not context_rest or not baseline_rest:
                                continue
                            context_errors.append(abs(median(context_rest) - value) / value)
                            baseline_errors.append(abs(median(baseline_rest) - value) / value)
                        if context_errors and median(context_errors) >= median(baseline_errors):
                            samples = baseline
                            use_context = False
                    elif relative_mad(contextual_samples) > relative_mad(baseline):
                        samples = baseline
                        use_context = False
            predicted_unit = median(samples)
            estimate = predicted_unit * servings if servings else predicted_unit
            error = abs(estimate - actual) / actual
            covered += 1
            method = "contextual" if use_context else "fallback"
            context_counts[method] += 1
            method_stats[method]["covered"] += 1
            method_stats[method]["within_10"] += error <= .10
            method_stats[method]["within_25"] += error <= .25
            group["covered"] += 1
            errors.append(error)
            within_10 += error <= .10
            within_25 += error <= .25
            within_50 += error <= .50
            group["within_25"] += error <= .25
            spread = sorted(abs(x - predicted_unit) / predicted_unit for x in samples)
            ingredient_category = category(name)
            variable = ingredient_category in ("sauces_condiments", "oils_fats", "dairy", "seasoning")
            # Condiments vary substantially between recipes even when the median
            # agrees. Require a larger and tighter distribution before labeling
            # a condiment estimate high-consensus. This is validation-only.
            if ingredient_category == "sauces_condiments":
                min_samples, median_limit, tail_limit, tail_fraction = 30, .025, .05, .9
            elif variable:
                min_samples, median_limit, tail_limit, tail_fraction = 20, .05, .10, .8
            else:
                min_samples, median_limit, tail_limit, tail_fraction = 10, .10, .25, .8
            consensus = (len(samples) >= min_samples
                         and median(spread) <= median_limit
                         and spread[int(tail_fraction * (len(spread) - 1))] <= tail_limit)
            if consensus:
                method_stats[method]["high_consensus"] += 1
                method_stats[method]["high_consensus_within_25"] += error <= .25
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
                                 "serving_normalized": bool(servings), "recipe_context": context,
                                 "match_method": "contextual" if use_context else "fallback",
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
            "context_matching": context_counts,
            "method_accuracy": {method: {
                "covered": stats["covered"],
                "within_10_percent": round(stats["within_10"] / stats["covered"], 3) if stats["covered"] else None,
                "within_25_percent": round(stats["within_25"] / stats["covered"], 3) if stats["covered"] else None,
                "high_consensus": stats["high_consensus"],
                "high_consensus_within_25_percent": round(stats["high_consensus_within_25"] / stats["high_consensus"], 3) if stats["high_consensus"] else None,
            } for method, stats in method_stats.items()},
            "examples": examples, "applied_changes": 0}
