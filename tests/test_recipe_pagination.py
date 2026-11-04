"""Regression checks for opt-in server-side recipe browsing."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from server import recipe_page_matches


class RecipePageFilterTests(unittest.TestCase):
    def setUp(self):
        self.recipe = {"id": 12, "name": "Chicken Parmesan", "country": "US"}

    def test_search_and_country(self):
        self.assertTrue(recipe_page_matches(self.recipe, "chicken", "US"))
        self.assertTrue(recipe_page_matches(self.recipe, "us", "ALL"))
        self.assertFalse(recipe_page_matches(self.recipe, "salmon", "US"))
        self.assertFalse(recipe_page_matches(self.recipe, "", "CA"))

    def test_protein_groups(self):
        self.assertTrue(recipe_page_matches(self.recipe, protein="chicken"))
        self.assertFalse(recipe_page_matches(self.recipe, protein="beef"))
        self.assertTrue(recipe_page_matches({"name": "Tomato Risotto", "country": "IT"}, protein="other"))

    def test_other_country(self):
        self.assertFalse(recipe_page_matches(self.recipe, country="OTHER"))
        self.assertTrue(recipe_page_matches({"name": "Risotto", "country": "IT"}, country="OTHER"))


if __name__ == "__main__":
    unittest.main()
