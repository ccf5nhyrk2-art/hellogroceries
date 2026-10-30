# HelloGroceries: Cloudflare Access and multi-household deployment plan

> **Status: instructions / implementation requirements, not an enabled feature.** The current production `latest` image remains the original single-household app. Do not assume the features below are live until implemented, tested, and explicitly released.

## Desired user experience

- Preserve existing `/data/shopping.db`, recipes, pantry, and shopping lists.
- First visit: administrator creates their account **in the browser**, with no SSH, Docker logs, bootstrap token, or secret URL.
- Administrator can generate single-use, expiring invitation links in a web admin page and send them by text/iMessage.
- Invited friends register with email/password and each gets an independent private household.
- No outbound email provider or mailbox. Password reset must not claim to send email; design a secure administrator-assisted reset workflow if needed.
- Deployment remains OpenMediaVault Docker Compose, `ghcr.io/ccf5nhyrk2-art/hellogroceries:latest`, updated with **Pull → Up**.
- Cloudflare Tunnel and Zero Trust Access already protect `groceries.greenlee.uk`.

## Required Cloudflare settings (not secrets)

In **Cloudflare Zero Trust → Access → Applications → HelloGroceries**, retrieve:

1. **Team domain:** `greenlee1.cloudflareaccess.com` (confirmed in the Cloudflare Access login page).
2. **Application AUD tag:** `e28f3f8c9c8c1f038f18c62920f6279d1a493cfa0081ea8d8de1d6b6d1060b0b` (provided by the application owner).

Configure these as server-side environment variables, `HG_CF_TEAM_DOMAIN=greenlee1.cloudflareaccess.com` and `HG_CF_AUD=e28f3f8c9c8c1f038f18c62920f6279d1a493cfa0081ea8d8de1d6b6d1060b0b`. The application must explicitly read and verify these values before first-admin setup is enabled. A separate owner email allowlist (`HG_ADMIN_EMAIL`) should determine who can claim the first admin account; Access membership alone may include invited friends. Confirm the intended administrator email before activation. The Cloudflare Access application is named `groceries`, hostname `groceries.greenlee.uk`, with policy `HelloGroceries Admin`. The owner confirmed a private browsing window now shows Cloudflare's email-code login screen.

## Mandatory server-side security

- Verify the **cryptographic signature** of `Cf-Access-Jwt-Assertion` using the team's Cloudflare Access JWKS at `https://<team-domain>/cdn-cgi/access/certs`. Validate issuer, audience, expiration, token algorithm, and identity/email. Never trust `Cf-Access-Authenticated-User-Email` by itself.
- Treat all forwarded HTTP headers as untrusted until the JWT is verified: Docker port 8088 may be reachable directly on the LAN.
- Fail closed when Cloudflare configuration is missing/invalid; never silently allow public first-admin registration. Restrict first-admin creation to the verified `HG_ADMIN_EMAIL` and ensure the submitted account email matches the verified identity.
- One-time admin creation must be atomic and race-safe. Never overwrite the existing administrator.
- Protect all household data reads/writes with server-side membership checks; test cross-household isolation across every endpoint (including exports, backups, imports, recipes/photos where user-specific).
- Session cookies: HttpOnly, Secure when HTTPS, SameSite; CSRF protection on state-changing requests; login rate limiting; logout and session invalidation; strict origin checks.
- Invitations: administrator-only issuance, strong random tokens, hashed at rest, single use, short expiry, email binding, and safe redemption. Invitation URL fragments or query strings must not leak via logs or referrers.
- Do not expose account secrets in logs or the browser. Add security headers and avoid serving hidden files under `/data`.
- Preserve legacy state in the original table, copy it only into the first administrator household, and maintain a tested rollback path.
- Run automated tests including unauthenticated access, malformed/forged/expired JWTs, incorrect audience/issuer, direct-LAN header spoofing, unauthorized invites, CSRF, and household isolation.

## Deployment and rollback

1. Back up `/opt/hellofresh-grocery/data` (particularly `shopping.db`) before enabling accounts.
2. Implement and test all security requirements on a development branch. Keep the original image available as a rollback point.
3. Only after checks pass, merge to `main` to publish `latest`. Confirm GitHub Actions image publishing succeeded.
4. In OMV **Services → Compose → Files**, select the HelloGroceries stack, **Pull → Up**; preserve the existing bind mount `/opt/hellofresh-grocery/data:/data`.
5. Verify first admin setup, existing data, invite creation/redemption, and private household separation before sharing with friends.
6. If rolling back, restore the prior image; **do not delete the data folder**. Account-era changes to the DB must be handled cautiously.

## What not to do

- Do **not** remove the bootstrap-token requirement from the old account code until verified Cloudflare Access JWT bootstrap replaces it.
- Do **not** publish unfinished account authentication to `latest`.
- Do **not** promise automatic invitation or reset emails; the selected approach is manual invitation links.
