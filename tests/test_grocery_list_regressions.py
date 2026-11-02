"""Isolated grocery-list regression tests without starting the web server."""
import ast
import math
import pathlib
import re
import unittest

SOURCE = pathlib.Path(__file__).resolve().parents[1] / "app" / "server.py"
tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in ("parse_grocery_measure", "grocery_display_name", "rebuild_grocery_list")]
ns = {"math": math, "re": re}
exec(compile(ast.Module(body=functions, type_ignores=[]), str(SOURCE), "exec"), ns)


def make_state(pantry=None):
    return {"selected": {"1": 2, "2": 2}, "recipe_ingredient_corrections": {},
            "manual": [], "pantry": [], "pantry_inventory": pantry or {}, "list": []}


def generate(recipes, state):
    ns["allrecipes"] = lambda: recipes
    ns["rebuild_grocery_list"](state)
    return {item["name"]: item for item in state["list"]}


class GroceryListRegressionTests(unittest.TestCase):
    def test_shared_ingredients_combine_and_keep_attribution(self):
        recipes = [
            {"id": 1, "name": "A", "source_servings": 2, "ingredients": {"Limes": 1}},
            {"id": 2, "name": "B", "source_servings": 2, "ingredients": {"Limes": 1}},
        ]
        result = generate(recipes, make_state())
        self.assertEqual(result["Limes"]["quantity"], 2)
        self.assertEqual(len(result["Limes"]["sources"]), 2)

    def test_case_variants_merge_without_losing_recipe_sources(self):
        recipes = [
            {"id": 1, "name": "A", "source_servings": 2, "ingredients": {"Limes": 1}},
            {"id": 2, "name": "B", "source_servings": 2, "ingredients": {"limes": 1}},
        ]
        result = generate(recipes, make_state())
        self.assertEqual(len(result), 1)
        item = next(iter(result.values()))
        self.assertEqual(item["quantity"], 2)
        self.assertEqual(len(item["sources"]), 2)

    def test_shiitake_spelling_normalized_in_display(self):
        recipes = [
            {"id": 1, "name": "A", "source_servings": 2, "ingredients": {"4 ounces Shitake Mushrooms": 1}},
            {"id": 2, "name": "B", "source_servings": 2, "ingredients": {"4 ounces Shiitake Mushrooms": 1}},
        ]
        result = generate(recipes, make_state())
        self.assertEqual(len(result), 1)
        self.assertTrue(any("Shiitake" in name for name in result))
        self.assertEqual(len(next(iter(result.values()))["sources"]), 2)

    def test_bell_pepper_singular_plural_merge(self):
        recipes = [
            {"id": 1, "name": "A", "source_servings": 2, "ingredients": {"Green Bell Pepper": 1}},
            {"id": 2, "name": "B", "source_servings": 2, "ingredients": {"Green Bell Peppers": 1}},
        ]
        result = generate(recipes, make_state())
        self.assertEqual(len(result), 1)
        self.assertEqual(next(iter(result.values()))["quantity"], 2)
        self.assertEqual(len(next(iter(result.values()))["sources"]), 2)

    def test_unspecified_sour_cream_not_added_to_measured_amount(self):
        recipes = [
            {"id": 1, "name": "A", "source_servings": 2, "ingredients": {"Sour Cream": 1}},
            {"id": 2, "name": "B", "source_servings": 2, "ingredients": {"4 tablespoons Sour Cream": 1}},
        ]
        result = generate(recipes, make_state())
        self.assertEqual(len(result), 2)

    def test_herb_butter_not_merged_with_regular_butter(self):
        recipes = [
            {"id": 1, "name": "A", "source_servings": 2, "ingredients": {"Herb Butter": 1}},
            {"id": 2, "name": "B", "source_servings": 2, "ingredients": {"Butter": 1}},
        ]
        result = generate(recipes, make_state())
        self.assertIn("Herb Butter", result)
        self.assertIn("Butter", result)

    def test_pantry_subtraction_never_uses_stock_twice(self):
        recipes = [
            {"id": 1, "name": "A", "source_servings": 2, "ingredients": {"2 cups Milk": 1}},
            {"id": 2, "name": "B", "source_servings": 2, "ingredients": {"1 cup Milk": 1}},
        ]
        result = generate(recipes, make_state({"Milk|cup": 1}))
        # Two recipes need 3 cups; pantry supplies only 1 cup total.
        required = sum(ns["parse_grocery_measure"](name)["amount"] * item["quantity"]
                       for name, item in result.items() if "Milk" in name)
        self.assertAlmostEqual(required, 2.0)

    def test_fractional_measurement(self):
        recipes = [{"id": 1, "name": "A", "source_servings": 2,
                    "ingredients": {"¼ ounce Cilantro": 1}}]
        state = make_state()
        state["selected"] = {"1": 2}
        result = generate(recipes, state)
        self.assertTrue(any("Cilantro" in name for name in result))
        self.assertAlmostEqual(sum(item["quantity"] for name, item in result.items()
                                   if "Cilantro" in name), 0.25)


if __name__ == "__main__":
    unittest.main()
