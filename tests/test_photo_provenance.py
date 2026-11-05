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


    def test_verified_imported_images_request_jpeg_first(self):
        source = SOURCE.read_text()
        tree = ast.parse(source)
        downloader = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "download_photo")
        resolver = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "resolve_photo")
        self.assertIn("prefer_jpeg", [a.arg for a in downloader.args.args])
        # The JPEG preference is limited to provenance-verified imported URLs.
        calls = [n for n in ast.walk(resolver) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name) and n.func.id == "download_photo"]
        self.assertTrue(any(any(k.arg == "prefer_jpeg" for k in call.keywords) for call in calls))
        self.assertIn("image/jpeg,image/png", ast.get_source_segment(source, downloader))
        self.assertIn("digest in photo_rejected_digests", ast.get_source_segment(source, downloader))
        self.assertIn("Identical to cached recipe", ast.get_source_segment(source, downloader))

    def test_unverified_imported_images_do_not_get_jpeg_override(self):
        source = SOURCE.read_text()
        resolver = next(n for n in ast.parse(source).body
                        if isinstance(n, ast.FunctionDef) and n.name == "resolve_photo")
        override = next(k.value for n in ast.walk(resolver)
                        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                        and n.func.id == "download_photo" for k in n.keywords
                        if k.arg == "prefer_jpeg")
        expr = compile(ast.Expression(override), str(SOURCE), "eval")
        self.assertFalse(eval(expr, {}, {"rid": 1012949, "trusted": "", "url": "https://img.hellofresh.com/x.jpg"}))
        self.assertFalse(eval(expr, {}, {"rid": 1012949, "trusted": "https://img.hellofresh.com/owned.jpg", "url": "https://img.hellofresh.com/other.jpg"}))
        self.assertTrue(eval(expr, {}, {"rid": 1012949, "trusted": "https://img.hellofresh.com/owned.jpg", "url": "https://img.hellofresh.com/owned.jpg"}))
        self.assertFalse(eval(expr, {}, {"rid": 99, "trusted": "https://img.hellofresh.com/owned.jpg", "url": "https://img.hellofresh.com/owned.jpg"}))


    def test_force_jpeg_transform_only_on_official_cdn(self):
        source = SOURCE.read_text()
        node = next(n for n in ast.parse(source).body
                    if isinstance(n, ast.FunctionDef) and n.name == "force_verified_photo_jpeg_url")
        scope = {"urllib": urllib}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), "exec"), scope)
        force = scope["force_verified_photo_jpeg_url"]
        original = "https://img.hellofresh.com/f_auto,fl_lossy,h_640,q_auto,w_1200/hellofresh_s3/image/61cb2c09813874009620da38-4168f376.jpg"
        expected = original.replace("/f_auto,", "/f_jpg,")
        self.assertEqual(force(original), expected)
        self.assertEqual(force(expected), expected)
        self.assertEqual(force(original.replace("img.hellofresh.com", "example.com")), original.replace("img.hellofresh.com", "example.com"))
        self.assertEqual(force("https://img.hellofresh.com/hellofresh_s3/image/foo.jpg"), "https://img.hellofresh.com/hellofresh_s3/image/foo.jpg")

    def test_downloader_forces_jpeg_only_when_verified(self):
        source = SOURCE.read_text()
        node = next(n for n in ast.parse(source).body
                    if isinstance(n, ast.FunctionDef) and n.name == "download_photo")
        self.assertIn("if prefer_jpeg:url=force_verified_photo_jpeg_url(url)", ast.get_source_segment(source, node))
        self.assertIn("digest in photo_rejected_digests", ast.get_source_segment(source, node))

if __name__ == "__main__":
    unittest.main()
