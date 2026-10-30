"""Per-installation identity and household schema for HelloGroceries.

This migration is additive. It does not change or remove the legacy state row.
Authentication and authorization must be enforced before exposing household APIs.
"""
import sqlite3

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
