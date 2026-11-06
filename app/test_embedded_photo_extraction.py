"""Regression tests for HelloFresh embedded-image extraction."""
import ast
import html
import re
import unittest
import urllib.parse
from pathlib import Path

SOURCE=Path(__file__).with_name("server.py").read_text()
tree=ast.parse(SOURCE)
names={"official_images_from_embedded_recipe","clean_official_photo_candidate","complete_recipe_photo_url"}
functions=[node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name in names]
scope={"html":html,"re":re,"urllib":urllib}
# Keep network safety checks mocked; test extraction and image asset validation.
scope["allowed_photo_url"]=lambda url:urllib.parse.urlsplit(url).hostname in ("media.hellofresh.com","img.hellofresh.com","cdn.hellofresh.com")
exec(compile(ast.Module(body=functions,type_ignores=[]),"<server photo extractors>","exec"),scope)
extract=scope["official_images_from_embedded_recipe"]

class EmbeddedPhotoTests(unittest.TestCase):
 def setUp(self):
  self.rid="68d653919ed4d6794189e8ad"
  self.recipe={"url":"https://www.hellofresh.com/recipes/example-"+self.rid}
 def test_complete_asset_url_is_not_truncated(self):
  image="https://media.hellofresh.com/w_1200,q_auto,f_auto,c_limit,fl_lossy/recipes/image/654a9e5533e8a37769159abd-32b1a53a.jpeg"
  page='{"recipeId":"'+self.rid+'","image":"'+image+'"}'
  self.assertEqual(extract(self.recipe,page),[image])
 def test_truncated_url_is_rejected(self):
  page='{"recipeId":"'+self.rid+'","image":"https://media.hellofresh.com/hellofre"}'
  self.assertEqual(extract(self.recipe,page),[])
 def test_unrelated_page_identity_is_rejected(self):
  page='{"recipeId":"aaaaaaaaaaaaaaaaaaaaaaaa","image":"https://media.hellofresh.com/recipes/image/test.jpg"}'
  self.assertEqual(extract(self.recipe,page),[])
 def test_multiple_candidates_are_not_guessed(self):
  page='{"recipeId":"'+self.rid+'","image":["https://media.hellofresh.com/recipes/image/a.jpg","https://media.hellofresh.com/recipes/image/b.jpg"]}'
  self.assertEqual(extract(self.recipe,page),[])

if __name__=="__main__":unittest.main()
