import unittest
from walmart_opd import prepare_items, add_to_cart, WalmartAccessPending


class WalmartOpdTests(unittest.TestCase):
    def test_normalizes_precalculated_items(self):
        result = prepare_items([
            {"name": " Butter ", "quantity": 2, "unit": "tablespoon"},
            {"name": "Salt", "quantity": 0, "unit": "teaspoon"},
            {"name": "", "quantity": 1, "unit": "piece"},
            {"name": "Eggs", "quantity": -2, "unit": "each"},
        ])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].ingredient, "Butter")
        self.assertEqual(result[0].quantity, 2.0)
        self.assertEqual(result[0].unit, "tablespoon")

    def test_no_cart_access_while_pending(self):
        with self.assertRaises(WalmartAccessPending):
            add_to_cart([])


if __name__ == "__main__":
    unittest.main()
