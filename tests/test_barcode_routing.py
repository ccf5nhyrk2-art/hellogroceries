import ast
import pathlib
import unittest

SOURCE = pathlib.Path(__file__).resolve().parents[1] / "app" / "server.py"

class BarcodeRoutingTests(unittest.TestCase):
    def test_lookup_is_registered_under_get_not_post(self):
        tree = ast.parse(SOURCE.read_text())
        handler = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Handler")
        methods = {node.name: ast.get_source_segment(SOURCE.read_text(), node) for node in handler.body if isinstance(node, ast.FunctionDef)}
        route = "self.path.startswith('/api/pantry/barcode?')"
        self.assertIn(route, methods["do_GET"])
        self.assertNotIn(route, methods["do_POST"])
        self.assertIn("self.path=='/api/pantry/barcode/save'", methods["do_POST"])

if __name__ == "__main__":
    unittest.main()
