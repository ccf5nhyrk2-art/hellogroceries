"""Per-installation identity and household schema for HelloGroceries.

This migration is additive. It does not change or remove the legacy state row.
Authentication and authorization must be enforced before exposing household APIs.
"""
import sqlite3
import hashlib
import hmac
import secrets
import datetime
import json
import re

SCHEMA = """
CREATE TABLE IF NOT EXISTS hg_users (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 email TEXT NOT NULL UNIQUE COLLATE NOCASE,
 password_hash TEXT NOT NULL,
 is_instance_admin INTEGER NOT NULL DEFAULT 0 CHECK(is_instance_admin IN (0,1)),
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS hg_households (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 name TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS hg_memberships (
 user_id INTEGER NOT NULL REFERENCES hg_users(id) ON DELETE CASCADE,
 household_id INTEGER NOT NULL REFERENCES hg_households(id) ON DELETE CASCADE,
 role TEXT NOT NULL CHECK(role IN ('owner','member')),
 PRIMARY KEY(user_id,household_id)
);
CREATE TABLE IF NOT EXISTS hg_household_state (
 household_id INTEGER PRIMARY KEY REFERENCES hg_households(id) ON DELETE CASCADE,
 payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hg_invitations (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 token_hash TEXT NOT NULL UNIQUE,
 email TEXT NOT NULL COLLATE NOCASE,
 household_id INTEGER REFERENCES hg_households(id) ON DELETE CASCADE,
 invited_by INTEGER NOT NULL REFERENCES hg_users(id),
 expires_at TEXT NOT NULL,
 redeemed_at TEXT,
 revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS hg_sessions (
 token_hash TEXT PRIMARY KEY,
 user_id INTEGER NOT NULL REFERENCES hg_users(id) ON DELETE CASCADE,
 expires_at TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS hg_one_instance_admin
 ON hg_users(is_instance_admin) WHERE is_instance_admin=1;
CREATE INDEX IF NOT EXISTS hg_memberships_household ON hg_memberships(household_id);
CREATE INDEX IF NOT EXISTS hg_invitations_email ON hg_invitations(email);
"""

def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)

def instance_needs_setup(conn: sqlite3.Connection) -> bool:
    ensure_schema(conn)
    return conn.execute("SELECT NOT EXISTS(SELECT 1 FROM hg_users WHERE is_instance_admin=1)").fetchone()[0] == 1

# Authentication primitives are framework-independent so they can be tested
# without starting the web server. All tokens are random; only hashes persist.
PASSWORD_ITERATIONS = 600_000
SESSION_HOURS = 24 * 14
INVITE_HOURS = 72

def password_hash(password):
    if not isinstance(password, str) or len(password) < 12 or len(password) > 1024:
        raise ValueError("Password must be 12–1024 characters")
    salt = secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256:{PASSWORD_ITERATIONS}:{salt.hex()}:{derived.hex()}"

def verify_password(password, encoded):
    try:
        scheme, rounds, salt, expected = encoded.split(":")
        if scheme != "pbkdf2_sha256" or not 100_000 <= int(rounds) <= 1_000_000:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(rounds))
        return hmac.compare_digest(actual, bytes.fromhex(expected))
    except (ValueError, TypeError, AttributeError):
        return False

def token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

def expiry(hours):
    return (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=hours)).isoformat()

def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def validate_email(email):
    if not isinstance(email, str):
        raise ValueError("Invalid email")
    email = email.strip().casefold()
    if len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise ValueError("Invalid email")
    return email

def create_first_admin(conn, email, password, household_name="My Household"):
    """Atomic, one-time bootstrap. Caller must verify a local bootstrap secret."""
    email = validate_email(email)
    if not isinstance(household_name, str) or not 1 <= len(household_name.strip()) <= 80:
        raise ValueError("Invalid household name")
    hashed = password_hash(password)
    ensure_schema(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute("SELECT 1 FROM hg_users LIMIT 1").fetchone():
            raise ValueError("Setup already completed")
        uid = conn.execute("INSERT INTO hg_users(email,password_hash,is_instance_admin) VALUES(?,?,1)", (email, hashed)).lastrowid
        hid = conn.execute("INSERT INTO hg_households(name) VALUES(?)", (household_name.strip(),)).lastrowid
        conn.execute("INSERT INTO hg_memberships(user_id,household_id,role) VALUES(?,?,'owner')", (uid, hid))
        # Copy, never remove, the legacy shopping state; only the first installation
        # household receives it. Existing users can roll back without data loss.
        legacy = conn.execute("SELECT payload FROM state WHERE id=1").fetchone()
        conn.execute("INSERT INTO hg_household_state(household_id,payload) VALUES(?,?)", (hid, legacy[0] if legacy else '{"selected":{},"list":[],"revision":0}'))
        conn.commit()
        return uid, hid
    except Exception:
        conn.rollback()
        raise

def authenticate(conn, email, password):
    email = validate_email(email)
    row = conn.execute("SELECT id,password_hash FROM hg_users WHERE email=?", (email,)).fetchone()
    if row is None or not verify_password(password, row[1]):
        raise ValueError("Invalid email or password")
    return row[0]

def create_session(conn, user_id):
    token = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO hg_sessions(token_hash,user_id,expires_at) VALUES(?,?,?)",
                 (token_hash(token), user_id, expiry(SESSION_HOURS)))
    return token

def session_user(conn, token):
    if not isinstance(token, str) or len(token) > 256:
        return None
    return conn.execute("""
        SELECT u.id,u.email,u.is_instance_admin
        FROM hg_sessions s JOIN hg_users u ON u.id=s.user_id
        WHERE s.token_hash=? AND s.expires_at>?
    """, (token_hash(token), now())).fetchone()

def revoke_session(conn, token):
    conn.execute("DELETE FROM hg_sessions WHERE token_hash=?", (token_hash(token),))

def household_access(conn, user_id, household_id):
    return conn.execute("""
        SELECT role FROM hg_memberships
        WHERE user_id=? AND household_id=?
    """, (user_id, household_id)).fetchone()

def issue_invite(conn, admin_id, email, household_id=None):
    email = validate_email(email)
    admin = conn.execute("SELECT is_instance_admin FROM hg_users WHERE id=?", (admin_id,)).fetchone()
    if not admin or not admin[0]:
        raise PermissionError("Only an instance administrator may invite")
    if household_id is not None and not conn.execute("SELECT 1 FROM hg_households WHERE id=?", (household_id,)).fetchone():
        raise ValueError("Household not found")
    token = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO hg_invitations(token_hash,email,household_id,invited_by,expires_at) VALUES(?,?,?,?,?)",
                 (token_hash(token), email, household_id, admin_id, expiry(INVITE_HOURS)))
    return token

def redeem_invite(conn, token, email, password, new_household=None):
    email = validate_email(email)
    hashed = password_hash(password)
    conn.execute("BEGIN IMMEDIATE")
    try:
        invite = conn.execute("""
            SELECT id,household_id,email FROM hg_invitations
            WHERE token_hash=? AND redeemed_at IS NULL AND revoked_at IS NULL AND expires_at>?
        """, (token_hash(token), now())).fetchone()
        if not invite or invite[2] != email:
            raise ValueError("Invalid, expired or already-used invitation")
        if conn.execute("SELECT 1 FROM hg_users WHERE email=?", (email,)).fetchone():
            raise ValueError("Account already exists")
        uid = conn.execute("INSERT INTO hg_users(email,password_hash) VALUES(?,?)", (email, hashed)).lastrowid
        hid = invite[1]
        if hid is None:
            if not isinstance(new_household, str) or not 1 <= len(new_household.strip()) <= 80:
                raise ValueError("Household name required")
            hid = conn.execute("INSERT INTO hg_households(name) VALUES(?)", (new_household.strip(),)).lastrowid
            role = "owner"
            conn.execute("INSERT INTO hg_household_state(household_id,payload) VALUES(?,?)",
                         (hid, '{"selected":{},"list":[],"revision":0}'))
        else:
            role = "member"
        conn.execute("INSERT INTO hg_memberships(user_id,household_id,role) VALUES(?,?,?)", (uid, hid, role))
        conn.execute("UPDATE hg_invitations SET redeemed_at=? WHERE id=? AND redeemed_at IS NULL", (now(), invite[0]))
        conn.commit()
        return uid, hid
    except Exception:
        conn.rollback()
        raise
