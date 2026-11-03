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


def robust_prediction(samples, ingredient_category):
    """Training-only median, abstaining on strongly split quantity distributions.

    A median between two distinct common package sizes can be misleading.
    Require the middle half of observations to occupy a reasonably narrow band
    for variable ingredient groups. This never consults held-out answers.
    """
    values = sorted(float(x) for x in samples if math.isfinite(x) and x > 0)
    if not values:
        return None
    center = median(values)
    if ingredient_category in ("oils_fats", "dairy", "grain_or_starch"):
        if len(values) < 12:
            return None
        q1 = values[(len(values) - 1) // 4]
        q3 = values[(3 * (len(values) - 1)) // 4]
        # Reject ambiguous, multi-size distributions rather than pretending
        # their median is a reliably verified ingredient amount.
        if q3 / max(q1, 1e-9) > 1.75:
            return None
    return center


def evaluate(recipes, limit=5000, minimum_examples=5, prediction_policy="conservative"):
    if prediction_policy not in ("conservative", "median_baseline", "category_hybrid"):
        raise ValueError("Unknown prediction policy")
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
        if i % 10 in (0, 5):
            # Separate primary and independent holdouts by whole recipe.
            # Previously i % 5 == 0 made the independent cohort impossible.
            heldout.append((recipe.get("id"), servings, recipe_context(recipe), rows, "independent_check" if i % 10 == 5 else "primary"))
        else:
            for name, unit, amount in rows:
                # Never mix serving-normalized and unnormalized training examples.
                key = (name, unit, "per_serving" if servings else "raw", recipe_context(recipe))
                training[(name, unit, "per_serving" if servings else "raw")].append(amount / servings if servings else amount)
                contextual_training[key].append(amount / servings if servings else amount)
    # Stratify the held-out evaluation by category rather than taking only
    # the first N rows (which disproportionately samples low recipe IDs).
    # Entire recipes are held out before any training data is collected.
    heldout_by_category = defaultdict(list)
    for recipe_id, servings, context, rows, cohort in heldout:
        for name, unit, actual in rows:
            heldout_by_category[category(name)].append((recipe_id, servings, context, name, unit, actual, cohort))
    balanced_holdout = []
    category_names = sorted(heldout_by_category)
    offsets = {name: 0 for name in category_names}
    while len(balanced_holdout) < limit:
        added = False
        for group_name in category_names:
            entries = heldout_by_category[group_name]
            offset = offsets[group_name]
            if offset < len(entries):
                balanced_holdout.append(entries[offset])
                offsets[group_name] += 1
                added = True
                if len(balanced_holdout) >= limit:
                    break
        if not added:
            break
    checked = covered = within_10 = within_25 = within_50 = 0
    errors = []
    examples = []
    categories = defaultdict(lambda: {"tested": 0, "covered": 0, "within_25": 0, "high_consensus": 0, "high_consensus_within_25": 0})
    high_confidence = {"eligible": 0, "within_25": 0, "within_10": 0, "over_50": 0, "over_100": 0}
    context_counts = {"contextual": 0, "fallback": 0}
    method_stats = {method: {"covered": 0, "within_10": 0, "within_25": 0, "high_consensus": 0, "high_consensus_within_25": 0} for method in ("contextual", "fallback")}
    independent = {"tested": 0, "covered": 0, "high_consensus": 0, "within_25": 0, "over_50": 0, "over_100": 0}
    for recipe_id, servings, context, name, unit, actual, cohort in balanced_holdout:
            checked += 1
            if cohort == "independent_check":
                independent["tested"] += 1
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
                    if relative_mad(contextual_samples) > relative_mad(baseline):
                        samples = baseline
                        use_context = False
            # Hybrid is validation-only: keep cautious abstention for oils/dairy,
            # and test median coverage for grains and remaining categories.
            # This choice depends on ingredient category, not held-out answers.
            ingredient_group = category(name)
            use_robust = (prediction_policy == "conservative" or
                          (prediction_policy == "category_hybrid" and ingredient_group in ("oils_fats", "dairy")))
            predicted_unit = robust_prediction(samples, ingredient_group) if use_robust else median(samples)
            if predicted_unit is None:
                continue
            estimate = predicted_unit * servings if servings else predicted_unit
            error = abs(estimate - actual) / actual
            covered += 1
            if cohort == "independent_check":
                independent["covered"] += 1
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
            # Stricter consensus for categories that failed the larger holdout.
            if ingredient_category == "seasoning":
                min_samples, median_limit, tail_limit, tail_fraction = 35, .025, .05, .95
            elif ingredient_category == "other":
                min_samples, median_limit, tail_limit, tail_fraction = 25, .05, .10, .9
            consensus = (len(samples) >= min_samples
                         and median(spread) <= median_limit
                         and spread[int(tail_fraction * (len(spread) - 1))] <= tail_limit)
            if consensus:
                method_stats[method]["high_consensus"] += 1
                method_stats[method]["high_consensus_within_25"] += error <= .25
                group["high_consensus"] += 1
                group["high_consensus_within_25"] += error <= .25
                if cohort == "independent_check":
                    independent["high_consensus"] += 1
                    independent["within_25"] += error <= .25
                    independent["over_50"] += error > .50
                    independent["over_100"] += error > 1.0
                high_confidence["eligible"] += 1
                high_confidence["within_25"] += error <= .25
                high_confidence["within_10"] += error <= .10
                high_confidence["over_50"] += error > .50
                high_confidence["over_100"] += error > 1.0
            if len(examples) < 25:
                examples.append({"recipe_id": str(recipe_id), "ingredient": name,
                                 "unit": unit, "actual": actual,
                                 "estimated": round(estimate, 4),
                                 "relative_error": round(error, 3),
                                 "training_examples": len(samples),
                                 "serving_normalized": bool(servings), "recipe_context": context,
                                 "match_method": "contextual" if use_context else "fallback",
                                 "high_consensus": consensus})
    return {"mode": "offline_validation_only", "prediction_policy": prediction_policy, "evaluated": checked, "evaluation_limit": limit,
            "holdout_strategy": "recipe_disjoint_category_round_robin_with_independent_cohort",
            "independent_check": {**independent,
                "within_25_percent": round(independent["within_25"] / independent["high_consensus"], 3) if independent["high_consensus"] else None,
                "over_50_percent": round(independent["over_50"] / independent["high_consensus"], 3) if independent["high_consensus"] else None,
                "over_100_percent": round(independent["over_100"] / independent["high_consensus"], 3) if independent["high_consensus"] else None},
            "heldout_available_by_category": {name: len(entries) for name, entries in sorted(heldout_by_category.items())},
            "covered": covered, "coverage": round(covered / checked, 3) if checked else 0,
            "within_10_percent": round(within_10 / covered, 3) if covered else None,
            "within_25_percent": round(within_25 / covered, 3) if covered else None,
            "within_50_percent": round(within_50 / covered, 3) if covered else None,
            "median_relative_error": round(median(errors), 3) if errors else None,
            "high_confidence": {
                "eligible": high_confidence["eligible"],
                "within_10_percent": round(high_confidence["within_10"] / high_confidence["eligible"], 3) if high_confidence["eligible"] else None,
                "within_25_percent": round(high_confidence["within_25"] / high_confidence["eligible"], 3) if high_confidence["eligible"] else None,
                "over_50_percent": round(high_confidence["over_50"] / high_confidence["eligible"], 3) if high_confidence["eligible"] else None,
                "over_100_percent": round(high_confidence["over_100"] / high_confidence["eligible"], 3) if high_confidence["eligible"] else None},
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


def compare_prediction_policies(recipes, limit=5000, minimum_examples=5):
    """Read-only paired comparison on identical recipe-disjoint holdouts.

    The unfiltered median is a fixed reference, not a proposed rollout model.
    Accuracy and coverage must both be considered before changing policy.
    """
    recipes = list(recipes)
    baseline = evaluate(recipes, limit=limit, minimum_examples=minimum_examples,
                        prediction_policy="median_baseline")
    candidate = evaluate(recipes, limit=limit, minimum_examples=minimum_examples,
                         prediction_policy="conservative")
    hybrid = evaluate(recipes, limit=limit, minimum_examples=minimum_examples,
                      prediction_policy="category_hybrid")
    categories = {}
    for name in sorted(set(baseline["categories"]) | set(candidate["categories"]) | set(hybrid["categories"])):
        old = baseline["categories"].get(name, {})
        new = candidate["categories"].get(name, {})
        mixed = hybrid["categories"].get(name, {})
        categories[name] = {
            "baseline_covered": old.get("covered", 0),
            "candidate_covered": new.get("covered", 0),
            "baseline_within_25_percent": old.get("within_25_percent"),
            "candidate_within_25_percent": new.get("within_25_percent"),
            "hybrid_covered": mixed.get("covered", 0),
            "hybrid_within_25_percent": mixed.get("within_25_percent"),
        }
    return {
        "mode": "offline_validation_only", "applied_changes": 0,
        "same_holdout": baseline["evaluated"] == candidate["evaluated"],
        "baseline": {k: baseline[k] for k in ("evaluated", "covered", "coverage", "within_25_percent", "high_confidence", "independent_check")},
        "candidate": {k: candidate[k] for k in ("evaluated", "covered", "coverage", "within_25_percent", "high_confidence", "independent_check")},
        "hybrid": {k: hybrid[k] for k in ("evaluated", "covered", "coverage", "within_25_percent", "high_confidence", "independent_check")},
        "priority_categories": {k: categories[k] for k in ("oils_fats", "dairy", "grain_or_starch") if k in categories},
        "categories": categories,
    }
