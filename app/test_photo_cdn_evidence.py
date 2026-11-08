"""Regression checks for read-only official CDN variant identity."""
import unittest
from unittest.mock import patch
import audit_photo_cdn_evidence as audit


class CDNVariantsTest(unittest.TestCase):
    def test_variants_preserve_exact_asset(self):
        url = "https://img.hellofresh.com/f_auto,fl_lossy,h_640,q_auto,w_1200/hellofresh_s3/image/63752d6a2108d2b04c0fe3bc-696f5875-f5143d88.jpg"
        candidates = audit.variants(url)
        self.assertGreater(len(candidates), 2)
        self.assertEqual(len(candidates), len(set(candidates)))
        self.assertEqual({audit.asset_key(u) for u in candidates}, {audit.asset_key(url)})
        self.assertTrue(any("f_jpg" in u for u in candidates))
        self.assertTrue(any("f_webp" in u for u in candidates))

    def test_no_unbound_asset(self):
        self.assertEqual(audit.variants("https://img.hellofresh.com/other/image.jpg"), [])
        self.assertEqual(audit.variants("http://example.com/recipes/image/x.jpg"), [])

    def test_fetch_rejects_unsafe_url_before_network(self):
        with patch.object(audit.urllib.request, "build_opener") as opener:
            self.assertEqual(audit.fetch("https://example.com/x.jpg")["status"], "unsafe_url")
            opener.assert_not_called()


if __name__ == "__main__":
    unittest.main()
