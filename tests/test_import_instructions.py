"""Offline regression tests for the HelloFresh URL importer.

Run from the repository root:
    python -m unittest discover -s tests -v
"""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import server

URL = "https://www.hellofresh.com/recipes/test-recipe-123"
STEPS = [
    "Rinse the vegetables and cut the carrots into small pieces.",
    "Heat the pan and cook the vegetables until they become tender.",
    "Combine the cooked ingredients and serve the finished dish."
]


class FakeResponse:
    def __init__(self, page, url=URL):
        self.page = page.encode("utf-8")
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def geturl(self):
        return self.url

    def read(self, size=-1):
        return self.page[:size]


def page_for(steps, extra=None):
    recipe = {
        "@context": "https://schema.org",
        "@type": "Recipe",
        "name": "Sample Vegetable Dinner",
        "recipeIngredient": ["2 carrots", "1 onion", "1 cup rice"],
        "recipeInstructions": steps,
    }
    data = [recipe] + (extra or [])
    return '<html><script type="application/ld+json">' + json.dumps(data) + '</script></html>'


class ImportInstructionsTests(unittest.TestCase):
    def import_page(self, page):
        with patch.object(server.urllib.request, "urlopen", return_value=FakeResponse(page)):
            return server.recipe_from_url(URL)

    def test_valid_steps_imported_and_url_preserved(self):
        recipe = self.import_page(page_for([
            {"@type": "HowToStep", "text": step} for step in STEPS
        ]))
        self.assertEqual(recipe.get("instructions"), STEPS)
        self.assertEqual(recipe["instructions_url"], URL)
        self.assertEqual(recipe["url"], URL)

    def test_missing_steps_not_fabricated(self):
        recipe = self.import_page(page_for([]))
        self.assertFalse(recipe.get("instructions"))
        self.assertEqual(recipe["instructions_url"], URL)

    def test_invalid_single_step_not_accepted(self):
        recipe = self.import_page(page_for([{"@type": "HowToStep", "text": STEPS[0]}]))
        self.assertFalse(recipe.get("instructions"))

    def test_duplicate_steps_not_accepted(self):
        recipe = self.import_page(page_for([STEPS[0], STEPS[0]]))
        self.assertFalse(recipe.get("instructions"))

    def test_unrelated_recipe_steps_not_borrowed(self):
        recipe = self.import_page(page_for([], extra=[{
            "@type": "Recipe",
            "name": "Different Recipe",
            "recipeIngredient": ["1 carrot", "1 onion"],
            "recipeInstructions": STEPS
        }]))
        self.assertFalse(recipe.get("instructions"))

    def test_rejects_non_recipe_product(self):
        with self.assertRaisesRegex(ValueError, "non-recipe"):
            self.import_page(page_for(STEPS).replace("Test Vegetable Dinner", "Ready-Made Meal Box"))


if __name__ == "__main__":
    unittest.main()
