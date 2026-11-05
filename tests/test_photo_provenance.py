"""Regression tests for imported photo provenance (no network or database)."""
import ast
import pathlib
import re
import urllib.parse
import unittest

SOURCE = pathlib.Path(__file__).resolve().parents[1] / "app" / "server.py"
tree = ast.parse(SOURCE.read_text())
node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "trusted_imported_photo_url")
namespace = {"re": re, "urllib": urllib}
exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), "exec"), namespace)
namespace["clean_official_photo_candidate"] = lambda url: url
namespace["complete_recipe_photo_url"] = lambda url: url.startswith("https://img.hellofresh.com/")

class PhotoProvenanceTests(unittest.TestCase):
    def test_exact_recipe_id(self):
        r = {"url": "https://www.hellofresh.com/recipes/zucchini-bowls-63ae3c69bf4d4fa8b03054c0", "image": "https://img.hellofresh.com/hellofresh_s3/image/63ae3c69bf4d4fa8b03054c0-147e0778.jpg"}
        self.assertEqual(namespace["trusted_imported_photo_url"](r), r["image"])

    def test_descriptive_filename(self):
        r = {"url": "https://www.hellofresh.com/recipes/zucchini-tomato-couscous-bowls-63ae3c69bf4d4fa8b03054c0", "image": "https://img.hellofresh.com/hellofresh_s3/image/zucchini-tomato-couscous-bowls-19aed128.jpg"}
        self.assertEqual(namespace["trusted_imported_photo_url"](r), r["image"])

    def test_generic_and_unrelated_rejected(self):
        r = {"url": "https://www.hellofresh.com/recipes/turkey-wraps-63ae3c69bf4d4fa8b03054c0", "image": "https://img.hellofresh.com/hellofresh_s3/image/chicken-rice-bowl.jpg"}
        self.assertEqual(namespace["trusted_imported_photo_url"](r), "")
        r["image"] = "https://img.hellofresh.com/hellofresh_s3/image/default-recipe.jpg"
        self.assertEqual(namespace["trusted_imported_photo_url"](r), "")

    def test_jsonld_regex_matches_real_script(self):
        source = SOURCE.read_text()
        self.assertIn("application/ld", source)
        self.assertRegex(source, r"r'<script\\b")

if __name__ == "__main__":
    unittest.main()
