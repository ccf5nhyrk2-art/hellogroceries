"""Regression tests for grocery measurement parsing and ingredient identity."""
import ast
import pathlib
import unittest

SOURCE = pathlib.Path(__file__).resolve().parents[1] / "app" / "server.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
PARSER = next(node for node in TREE.body if isinstance(node, ast.FunctionDef) and node.name == "parse_grocery_measure")
# Compile only the pure parser to avoid starting the HTTP server or opening a database.
namespace = {"re": __import__("re")}
exec(compile(ast.Module(body=[PARSER], type_ignores=[]), str(SOURCE), "exec"), namespace)
parse = namespace["parse_grocery_measure"]


class GroceryMeasureTests(unittest.TestCase):
    def test_unicode_fraction(self):
        self.assertEqual(parse("¼ ounce Cilantro"), {"amount": 0.25, "unit": "ounce", "ingredient": "Cilantro"})

    def test_ascii_fraction(self):
        self.assertEqual(parse("1/2 cup Milk")["amount"], 0.5)

    def test_count_ingredient(self):
        self.assertEqual(parse("2 Tomatoes"), {"amount": 2.0, "unit": "count", "ingredient": "Tomatoes"})

    def test_cloves_and_thumb(self):
        self.assertEqual(parse("2 cloves Garlic")["unit"], "clove")
        self.assertEqual(parse("1 thumb Ginger")["ingredient"], "Ginger")

    def test_qualified_butter_remains_distinct(self):
        self.assertNotEqual(parse("1 cup Herb Butter")["ingredient"].casefold(), parse("1 cup Butter")["ingredient"].casefold())

    def test_no_quantity_is_unverified(self):
        self.assertIsNone(parse("Chili Pepper"))

    def test_unqualified_pepper_normalizes_but_varieties_remain_distinct(self):
        display_fn = next(node for node in TREE.body if isinstance(node, ast.FunctionDef) and node.name == "grocery_display_name")
        local = {"re": __import__("re")}
        exec(compile(ast.Module(body=[display_fn], type_ignores=[]), str(SOURCE), "exec"), local)
        normalize = local["grocery_display_name"]
        self.assertEqual(normalize("Pepper"), "Black Pepper")
        self.assertEqual(normalize("teaspoon Pepper"), "teaspoon Black Pepper")
        self.assertEqual(normalize("Black Pepper"), "Black Pepper")
        self.assertEqual(normalize("Crushed Red Pepper Flakes"), "Crushed Red Pepper Flakes")
        self.assertEqual(normalize("Green Bell Pepper"), "Green Bell Pepper")
        self.assertEqual(normalize("box Pepper"), "box Pepper")

    def test_invalid_input(self):
        self.assertIsNone(parse(None))
        self.assertIsNone(parse("not a measurement"))


if __name__ == "__main__":
    unittest.main()
