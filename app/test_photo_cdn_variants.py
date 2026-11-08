"""Regression checks for HelloFresh same-asset photo CDN fallbacks."""
import unittest
from unittest.mock import patch
import server


class PhotoCdnVariantTests(unittest.TestCase):
    def test_jpeg_variant_preserves_exact_asset(self):
        url = "https://img.hellofresh.com/f_auto,fl_lossy,h_640,q_auto,w_1200/hellofresh_s3/image/63752d6a2108d2b04c0fe3bc-696f5875-f5143d88.jpg"
        variants = server.official_photo_url_variants(url)
        self.assertIn("https://img.hellofresh.com/f_jpg,q_85,w_1200/hellofresh_s3/image/63752d6a2108d2b04c0fe3bc-696f5875-f5143d88.jpg", variants)
        self.assertTrue(all(server.complete_recipe_photo_url(v) for v in variants))
        self.assertTrue(all(v.endswith("/hellofresh_s3/image/63752d6a2108d2b04c0fe3bc-696f5875-f5143d88.jpg") for v in variants))

    def test_invalid_asset_not_rewritten(self):
        self.assertEqual(server.official_photo_url_variants("https://example.com/photo.jpg"), [])
        self.assertEqual(server.official_photo_url_variants("https://img.hellofresh.com/recipes/image/"), [])

    def test_variants_do_not_change_rejected_digest_list(self):
        url = "https://img.hellofresh.com/recipes/image/12345678.jpg"
        with patch.object(server, "photo_rejected_digests", set(["protected"])):
            server.official_photo_url_variants(url)
            self.assertEqual(server.photo_rejected_digests, {"protected"})


if __name__ == "__main__":
    unittest.main()
