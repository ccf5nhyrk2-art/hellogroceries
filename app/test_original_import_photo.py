"""Read-only smoke test for the original HelloFresh recipe importer.

Uses the same recipe_from_url() path as import_us_all.py. No database writes,
photo downloads, image recovery, or catalog modifications.
"""
import argparse
import json
import sys

from server import recipe_from_url, complete_recipe_photo_url, trusted_imported_photo_url

DEFAULT_URL = "https://www.hellofresh.com/recipes/10-mm-cajun-ranch-chicken-chop-salad-68d654a39ed4d6794189ee1e"


def check(url):
    recipe = recipe_from_url(url)
    photo = recipe.get("image") or ""
    complete = bool(complete_recipe_photo_url(photo))
    trusted = bool(trusted_imported_photo_url(recipe))
    result = {
        "recipe": recipe.get("name"),
        "source_url": recipe.get("url"),
        "imported_image_url": photo,
        "complete_image_url": complete,
        "identity_verified_from_url": trusted,
        "browse_eligible_after_cache": "requires a validated local photo",
    }
    print(json.dumps(result, indent=2))
    if not complete:
        print("FAIL: original import returned no complete image URL", file=sys.stderr)
        return 1
    if not trusted:
        print("REVIEW: original image URL is not independently tied to this recipe", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=DEFAULT_URL)
    args = parser.parse_args()
    sys.exit(check(args.url))
