# HelloGroceries — Staging release notes

## November 3, 2026 — Develop branch

**Release status:** Changes below are merged into `develop` for staging validation. This is **not** a production release. The running staging container must be refreshed after a successful Docker image build. A passing code-level test does not necessarily confirm an end-to-end workflow on the deployed site.

### New features
- **Share recipes, collections and meal plans:** Generate shareable links, use the device's native share sheet where supported, or copy a link. Signed-in recipients can import the shared recipe(s) into their account. Links encode recipe identifiers and selected sharing metadata; they are **not** revocable, private server-side invitations.
- **Standalone Home dashboard:** Desktop and mobile Home now prioritize four navigation tiles — Browse Recipes, Plan Meals, Shopping List and My Pantry. The redundant Find Nearby Stores shortcut has been removed from Home; stores remain available through dedicated navigation.

### Fixes and quality improvements
- **Pantry barcode lookup:** Corrected the barcode lookup route to respond to GET requests. Invalid barcodes should now return a validation error rather than a missing-route 404 once the updated server is deployed.
- **Home page isolation:** Legacy weekly planner, dinner suggestions, pantry controls and shopping sections should no longer appear beneath the Home tiles; those functions remain on their respective pages.
- **Regression coverage:** Added barcode route checks, share-link round-trip tests, Home layout checks and staging browser/API probes covering recipe browsing, shopping-list preview, pantry controls and administrator access.

### Validation and known limitations
- GitHub Actions for the Home layout pull request (#34) passed before merge; household security, recipe browser smoke checks, and grocery regression tests have also passed on recent `develop` revisions.
- Staging browser checks may fail against an older running container even after source fixes are merged; redeploy staging and rerun checks before interpreting failures as current.
- Recipe browsing API latency has been observed at roughly 15–17 seconds during staging probes. **Performance work remains open**.
- End-to-end sharing between two actual user accounts, and non-admin authorization using a separate test account, **remain to be verified**. Unit tests alone do not establish those outcomes.
- No claim is made that all existing UI workflows or data-import operations have been exhaustively tested.

### Related changes
- [PR #32 — Recipe, collection and meal-plan sharing](https://github.com/ccf5nhyrk2-art/hellogroceries/pull/32)
- [PR #33 — Barcode lookup and staging functional checks](https://github.com/ccf5nhyrk2-art/hellogroceries/pull/33)
- [PR #34 — Desktop Home layout fix](https://github.com/ccf5nhyrk2-art/hellogroceries/pull/34)

### Deployment
Use the `ghcr.io/ccf5nhyrk2-art/hellogroceries:develop` image for **staging only** after its build completes. Recreate the staging service using its existing OpenMediaVault Compose configuration. Do not deploy this release to `main`/production until staging validation is complete.
