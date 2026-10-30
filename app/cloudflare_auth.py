"""Verify Cloudflare Access JWT assertions before one-time administrator setup.

This is an identity boundary, not an HTTP-header allowlist. An untrusted
client can spoof headers when connecting directly to the Docker host port.
"""
import os
import re
import jwt
from jwt import PyJWKClient

TEAM = os.environ.get("HG_CF_TEAM_DOMAIN", "greenlee1.cloudflareaccess.com").strip().lower()
AUD = os.environ.get("HG_CF_AUD", "e28f3f8c9c8c1f038f18c62920f6279d1a493cfa0081ea8d8de1d6b6d1060b0b").strip()
if not re.fullmatch(r"[a-z0-9-]+\\.cloudflareaccess\\.com", TEAM):
    raise RuntimeError("Invalid Cloudflare Access team domain")
if not re.fullmatch(r"[0-9a-f]{64}", AUD):
    raise RuntimeError("Invalid Cloudflare Access audience")
ISSUER = "https://" + TEAM
KEYS = PyJWKClient(ISSUER + "/cdn-cgi/access/certs", cache_jwk_set=True, lifespan=300, timeout=5)

def verified_email(assertion):
    """Return verified email, or None on any authentication failure."""
    if not isinstance(assertion, str) or len(assertion) > 8192 or not assertion:
        return None
    try:
        key = KEYS.get_signing_key_from_jwt(assertion)
        claims = jwt.decode(
            assertion, key.key, algorithms=["RS256"],
            audience=AUD, issuer=ISSUER,
            options={"require": ["exp", "iat", "aud", "iss", "sub"]},
            leeway=30,
        )
        email = claims.get("email")
        if isinstance(email, str) and 3 <= len(email) <= 254 and "@" in email:
            return email.strip().casefold()
    except (jwt.PyJWTError, ValueError, TypeError, OSError):
        pass
    return None
