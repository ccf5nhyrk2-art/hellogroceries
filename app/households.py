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
CREATE TABLE IF NOT EXISTS hg_registration_requests (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 email TEXT NOT NULL UNIQUE COLLATE NOCASE,
 password_hash TEXT NOT NULL,
 household_name TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 reviewed_at TEXT
);
CREATE TABLE IF NOT EXISTS hg_join_requests (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 household_id INTEGER NOT NULL REFERENCES hg_households(id),
 user_id INTEGER NOT NULL REFERENCES hg_users(id),
 invited_by INTEGER NOT NULL REFERENCES hg_users(id),
 status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','accepted','declined')),
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS hg_join_requests_user ON hg_join_requests(user_id,status);
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
    conn.execute("CREATE TABLE IF NOT EXISTS hg_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("""CREATE TABLE IF NOT EXISTS hg_email_verifications (
        request_id INTEGER PRIMARY KEY REFERENCES hg_registration_requests(id) ON DELETE CASCADE,
        token_hash TEXT NOT NULL UNIQUE,
        expires_at TEXT NOT NULL,
        sent_at TEXT NOT NULL
    )""")
    # Additive migration: older installations already have hg_users.
    if not any(row[1] == "last_login_at" for row in conn.execute("PRAGMA table_info(hg_users)")):
        conn.execute("ALTER TABLE hg_users ADD COLUMN last_login_at TEXT")

def instance_needs_setup(conn: sqlite3.Connection) -> bool:
    ensure_schema(conn)
    return conn.execute("SELECT NOT EXISTS(SELECT 1 FROM hg_users WHERE is_instance_admin=1)").fetchone()[0] == 1

# Authentication primitives are framework-independent so they can be tested
# without starting the web server. All tokens are random; only hashes persist.
PASSWORD_ITERATIONS = 600_000
SESSION_HOURS = 24 * 14
INVITE_HOURS = 72

SESSION_DAY_OPTIONS = (1, 7, 14, 30)

def session_duration_days(conn):
    row = conn.execute("SELECT value FROM hg_settings WHERE key='session_days'").fetchone()
    try:
        value = int(row[0]) if row else 14
    except (TypeError, ValueError):
        value = 14
    return value if value in SESSION_DAY_OPTIONS else 14

def set_session_duration_days(conn, days):
    if type(days) is not int or days not in SESSION_DAY_OPTIONS:
        raise ValueError("Session duration must be 1, 7, 14, or 30 days")
    conn.execute("INSERT INTO hg_settings(key,value) VALUES('session_days',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(days),))

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
                 (token_hash(token), user_id, expiry(session_duration_days(conn) * 24)))
    conn.execute("UPDATE hg_users SET last_login_at=? WHERE id=?", (now(),user_id))
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

def request_registration(conn, email, password, household_name):
    email = validate_email(email)
    if not isinstance(household_name,str) or not 1 <= len(household_name.strip()) <= 80:
        raise ValueError("Household name must be 1–80 characters")
    hashed = password_hash(password)
    conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute("SELECT 1 FROM hg_users WHERE email=?", (email,)).fetchone():
            raise ValueError("Registration cannot be completed for this email")
        row=conn.execute("SELECT status FROM hg_registration_requests WHERE email=?", (email,)).fetchone()
        if row:
            raise ValueError("Registration request already received")
        conn.execute("INSERT INTO hg_registration_requests(email,password_hash,household_name) VALUES(?,?,?)",
                     (email,hashed,household_name.strip()))
        conn.commit()
    except Exception:
        conn.rollback()
        raise

def issue_registration_verification(conn,email):
    email=validate_email(email)
    row=conn.execute("SELECT id FROM hg_registration_requests WHERE email=? AND status='pending'",(email,)).fetchone()
    if not row: return None
    request_id=row[0]
    last=conn.execute("SELECT sent_at FROM hg_email_verifications WHERE request_id=?",(request_id,)).fetchone()
    if last and (datetime.datetime.now(datetime.timezone.utc)-datetime.datetime.fromisoformat(last[0])).total_seconds()<60:
        return None
    token=secrets.token_urlsafe(32)
    conn.execute("""INSERT INTO hg_email_verifications(request_id,token_hash,expires_at,sent_at)
        VALUES(?,?,?,?) ON CONFLICT(request_id) DO UPDATE SET
        token_hash=excluded.token_hash,expires_at=excluded.expires_at,sent_at=excluded.sent_at""",
        (request_id,token_hash(token),expiry(24),now()))
    return token

def activate_verified_registration(conn,token):
    if not isinstance(token,str) or len(token)>256 or len(token)<30:raise ValueError("Invalid verification link")
    conn.execute("BEGIN IMMEDIATE")
    try:
        row=conn.execute("""SELECT r.id,r.email,r.password_hash,r.household_name
            FROM hg_email_verifications v JOIN hg_registration_requests r ON r.id=v.request_id
            WHERE v.token_hash=? AND v.expires_at>? AND r.status='pending'""",(token_hash(token),now())).fetchone()
        if not row:raise ValueError("Verification link expired or already used. Request another email.")
        if conn.execute("SELECT 1 FROM hg_users WHERE email=?",(row[1],)).fetchone():
            raise ValueError("Account already exists")
        uid=conn.execute("INSERT INTO hg_users(email,password_hash) VALUES(?,?)",(row[1],row[2])).lastrowid
        hid=conn.execute("INSERT INTO hg_households(name) VALUES(?)",(row[3],)).lastrowid
        conn.execute("INSERT INTO hg_memberships(user_id,household_id,role) VALUES(?,?,'owner')",(uid,hid))
        conn.execute("INSERT INTO hg_household_state(household_id,payload) VALUES(?,?)",(hid,'{"selected":{},"list":[],"revision":0}'))
        conn.execute("UPDATE hg_registration_requests SET status='approved',reviewed_at=?,password_hash='' WHERE id=?",(now(),row[0]))
        conn.execute("DELETE FROM hg_email_verifications WHERE request_id=?",(row[0],))
        conn.commit()
        return uid
    except Exception:
        conn.rollback()
        raise

def pending_registrations(conn):
    return [dict(id=r[0],email=r[1],household=r[2],created_at=r[3])
            for r in conn.execute("SELECT id,email,household_name,created_at FROM hg_registration_requests WHERE status='pending' ORDER BY id")]

def review_registration(conn, admin_id, request_id, approve):
    if not conn.execute("SELECT 1 FROM hg_users WHERE id=? AND is_instance_admin=1",(admin_id,)).fetchone():
        raise PermissionError("Administrator required")
    if type(request_id) is not int or request_id < 1 or type(approve) is not bool:
        raise ValueError("Invalid review request")
    conn.execute("BEGIN IMMEDIATE")
    try:
        row=conn.execute("SELECT email,password_hash,household_name FROM hg_registration_requests WHERE id=? AND status='pending'",(request_id,)).fetchone()
        if not row: raise ValueError("Request is no longer pending")
        if approve:
            if conn.execute("SELECT 1 FROM hg_users WHERE email=?",(row[0],)).fetchone():
                raise ValueError("Account already exists")
            uid=conn.execute("INSERT INTO hg_users(email,password_hash) VALUES(?,?)",(row[0],row[1])).lastrowid
            hid=conn.execute("INSERT INTO hg_households(name) VALUES(?)",(row[2],)).lastrowid
            conn.execute("INSERT INTO hg_memberships(user_id,household_id,role) VALUES(?,?,'owner')",(uid,hid))
            conn.execute("INSERT INTO hg_household_state(household_id,payload) VALUES(?,?)",(hid,'{"selected":{},"list":[],"revision":0}'))
        conn.execute("UPDATE hg_registration_requests SET status=?,reviewed_at=?,password_hash='' WHERE id=?",
                     ('approved' if approve else 'rejected',now(),request_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise

def list_accounts(conn, admin_id):
    if not conn.execute("SELECT 1 FROM hg_users WHERE id=? AND is_instance_admin=1",(admin_id,)).fetchone():
        raise PermissionError("Administrator required")
    rows=conn.execute("""
        SELECT u.id,u.email,u.is_instance_admin,u.created_at,u.last_login_at,
               GROUP_CONCAT(h.name, ', ')
        FROM hg_users u
        LEFT JOIN hg_memberships m ON m.user_id=u.id
        LEFT JOIN hg_households h ON h.id=m.household_id
        GROUP BY u.id
        ORDER BY u.is_instance_admin DESC,u.created_at,u.id
    """).fetchall()
    last_activity={row[0]:row[1] for row in conn.execute("SELECT user_id,MAX(day) FROM hg_analytics GROUP BY user_id").fetchall()} if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hg_analytics'").fetchone() else {}
    active=conn.execute("""
        SELECT user_id, COUNT(*), MAX(created_at), MAX(expires_at)
        FROM hg_sessions WHERE expires_at>? GROUP BY user_id
    """,(now(),)).fetchall()
    by_user={row[0]:row for row in active}
    return [dict(id=r[0],email=r[1],admin=bool(r[2]),created_at=r[3],
                 last_login_at=r[4],households=r[5] or '',
                 last_active_at=last_activity.get(r[0]),active_sessions=by_user[r[0]][1] if r[0] in by_user else 0,
                 session_started_at=by_user[r[0]][2] if r[0] in by_user else None,
                 session_expires_at=by_user[r[0]][3] if r[0] in by_user else None) for r in rows]

def remove_account(conn, admin_id, user_id):
    if type(user_id) is not int or user_id < 1:
        raise ValueError("Invalid account ID")
    conn.execute("BEGIN IMMEDIATE")
    try:
        if not conn.execute("SELECT 1 FROM hg_users WHERE id=? AND is_instance_admin=1",(admin_id,)).fetchone():
            raise PermissionError("Administrator required")
        if user_id == admin_id:
            raise ValueError("You cannot remove your own administrator account")
        row=conn.execute("SELECT is_instance_admin FROM hg_users WHERE id=?",(user_id,)).fetchone()
        if row is None:raise ValueError("Account not found")
        if row[0]:raise ValueError("The instance administrator cannot be removed")
        # Do not cascade-delete household state: removing a login must not destroy
        # someone's grocery lists, pantry or other household data.
        conn.execute("DELETE FROM hg_memberships WHERE user_id=?",(user_id,))
        conn.execute("DELETE FROM hg_sessions WHERE user_id=?",(user_id,))
        conn.execute("DELETE FROM hg_invitations WHERE invited_by=?",(user_id,))
        conn.execute("DELETE FROM hg_users WHERE id=?",(user_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
