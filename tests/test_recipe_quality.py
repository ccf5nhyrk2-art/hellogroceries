import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from recipe_quality import inspect_ingredient, audit_catalog, verified_grocery_recovery


class RecipeQualityTests(unittest.TestCase):
    def test_explicit_amount(self):
        self.assertEqual(inspect_ingredient("2 cloves Garlic", 1)["status"], "verified")
        self.assertEqual(inspect_ingredient("4 ounces Salmon", 1)["status"], "verified")

    def test_explicit_count(self):
        self.assertEqual(inspect_ingredient("3 Grape Tomatoes", 1)["status"], "verified_count")

    def test_multiplier_is_not_quantity(self):
        for name in ("Garlic", "Salmon", "Grape Tomato", "Jalapeño Peppers"):
            self.assertEqual(inspect_ingredient(name, 2)["status"], "needs_source")

    def test_recovers_only_exact_unique_stored_source(self):
        recipe = {"id": 3, "ingredients": {"Salmon": 1},
                  "ingredient_text": ["2 ounces Salmon", "1 cup Rice"]}
        data = audit_catalog([recipe])
        self.assertEqual(data["counts"]["source_recovered"], 1)
        self.assertEqual(data["counts"]["needs_source"], 0)
        recipe["ingredient_text"].append("4 ounces Salmon")
        data = audit_catalog([recipe])
        self.assertEqual(data["counts"]["source_recovered"], 0)
        self.assertEqual(data["counts"]["needs_source"], 1)

    def test_safe_source_recovery_for_grocery_generation(self):
        recipe = {"id": 7, "ingredients": {"Salmon": 1},
                  "ingredient_text": ["2 ounces Salmon"]}
        recovered = verified_grocery_recovery(recipe, "Salmon", 1)
        self.assertEqual(recovered["amount"], 2)
        self.assertEqual(recovered["unit"], "ounce")
        self.assertEqual(recovered["confidence"], "verified_source")
        self.assertIsNone(verified_grocery_recovery(recipe, "Salmon", 2))
        recipe["ingredient_text"].append("4 ounces Salmon")
        self.assertIsNone(verified_grocery_recovery(recipe, "Salmon", 1))
        recipe["ingredient_text"] = ["1 package Salmon"]
        self.assertIsNone(verified_grocery_recovery(recipe, "Salmon", 1))

    def test_unit_only_is_not_mistaken_for_unknown_unit(self):
        for name in ("ounce Carrots", "tablespoon Mayonnaise",
                     "teaspoon (tsp) Black Pepper", "unit Salt"):
            if name.startswith("unit "):
                self.assertEqual(inspect_ingredient(name, 1)["status"], "needs_source")
            else:
                self.assertEqual(inspect_ingredient(name, 1)["status"], "unit_labeled")

    def test_source_matching_for_unit_only_ingredient(self):
        recipe = {"id": 9, "ingredients": {"ounce Salmon": 1},
                  "ingredient_text": ["4 ounces Salmon"]}
        self.assertEqual(audit_catalog([recipe])["counts"]["source_recovered"], 1)
        recovered = verified_grocery_recovery(recipe, "ounce Salmon", 1)
        self.assertEqual(recovered["amount"], 4)
        recipe["ingredient_text"].append("6 ounces Salmon")
        self.assertIsNone(verified_grocery_recovery(recipe, "ounce Salmon", 1))

    def test_unit_only_audit_counts(self):
        recipes = [{"id": 12, "ingredients": {
            "ounce Carrots": 1, "tablespoon Mayonnaise": 1,
            "teaspoon (tsp) Black Pepper": 1, "Salt": 1}}]
        counts = audit_catalog(recipes)["counts"]
        self.assertEqual(counts["unit_labeled"], 3)
        self.assertEqual(counts["needs_source"], 1)

    def test_plural_source_match(self):
        recipe = {"ingredients": {"ounce Carrots": 1}, "ingredient_text": ["6 ounces Carrot"]}
        self.assertEqual(verified_grocery_recovery(recipe, "ounce Carrots", 1)["amount"], 6)

    def test_source_abbreviations_and_mixed_fraction(self):
        recipe = {"ingredients": {"Salt": 1}, "ingredient_text": ["1 1/2 tsp. Salt"]}
        result = verified_grocery_recovery(recipe, "Salt", 1)
        self.assertIsNotNone(result)
        self.assertEqual(result["amount"], 1.5)
        self.assertEqual(result["unit"], "teaspoon")

    def test_to_taste_and_ranges_are_not_guessed(self):
        from recipe_quality import source_match_diagnostic
        for line in ("Salt, to taste", "1-2 tsp Salt", "2 (14 oz) cans Tomatoes"):
            recipe = {"ingredient_text": [line]}
            name = "Tomatoes" if "Tomatoes" in line else "Salt"
            self.assertIsNone(verified_grocery_recovery(recipe, name, 1))
        self.assertEqual(source_match_diagnostic({"ingredient_text": ["Salt, to taste"]}, "Salt")["status"], "to_taste_no_amount")

    def test_source_diagnostics_distinguish_missing_quantities(self):
        from recipe_quality import source_match_diagnostic
        unit_only = {"ingredient_text": ["teaspoon (tsp) Salt"]}
        plain = {"ingredient_text": ["Salt"]}
        self.assertEqual(source_match_diagnostic(unit_only, "teaspoon (tsp) Salt")["status"], "unit_without_quantity")
        self.assertEqual(source_match_diagnostic(plain, "Salt")["status"], "source_without_verified_quantity")
        self.assertIsNone(verified_grocery_recovery(unit_only, "teaspoon (tsp) Salt", 1))
        self.assertIsNone(verified_grocery_recovery(plain, "Salt", 1))

    def test_parenthetical_unit_alias_recovery(self):
        recipe = {"ingredient_text": ["2 teaspoons (tsp) Salt"]}
        recovered = verified_grocery_recovery(recipe, "teaspoon (tsp) Salt", 1)
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered["amount"], 2)
        self.assertEqual(recovered["unit"], "teaspoon")
        # A conflicting alias is not silently discarded.
        conflicting = {"ingredient_text": ["2 teaspoons (tbsp) Salt"]}
        self.assertIsNone(verified_grocery_recovery(conflicting, "Salt", 1))

    def test_invalid_factor(self):
        self.assertEqual(inspect_ingredient("Garlic", -1)["status"], "invalid")

    def test_audit_counts(self):
        data = audit_catalog([{"id": 1, "ingredients": {"2 cloves Garlic": 1, "Salmon": 1}}])
        self.assertEqual(data["counts"]["recipes"], 1)
        self.assertEqual(data["counts"]["verified"], 1)
        self.assertEqual(data["counts"]["needs_source"], 1)
        self.assertEqual(data["estimates_applied"], 0)


if __name__ == "__main__":
    unittest.main()
