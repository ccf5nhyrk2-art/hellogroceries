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
