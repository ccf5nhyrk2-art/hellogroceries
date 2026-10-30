import sqlite3
import unittest
from app import households as auth

class HouseholdSecurityTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("CREATE TABLE state(id INTEGER PRIMARY KEY, payload TEXT)")
        self.conn.execute("INSERT INTO state VALUES(1, ?)", ('{"selected":{"42":2},"list":[{"name":"Milk","quantity":1,"purchased":true}]}',))
        auth.ensure_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_bootstrap_preserves_existing_state_and_is_single_use(self):
        uid, hid = auth.create_first_admin(self.conn, "owner@example.test", "very-long-password-123", "Home")
        payload = self.conn.execute("SELECT payload FROM hg_household_state WHERE household_id=?", (hid,)).fetchone()[0]
        self.assertIn('"42"', payload)
        self.assertEqual(auth.authenticate(self.conn, "OWNER@example.test", "very-long-password-123"), uid)
        with self.assertRaises(ValueError):
            auth.create_first_admin(self.conn, "other@example.test", "very-long-password-123")

    def test_invite_is_single_use_and_household_isolated(self):
        uid, first = auth.create_first_admin(self.conn, "owner@example.test", "very-long-password-123")
        token = auth.issue_invite(self.conn, uid, "friend@example.test")
        self.conn.commit()
        friend, second = auth.redeem_invite(self.conn, token, "friend@example.test", "another-long-password-123", "Friend's Home")
        self.assertNotEqual(first, second)
        self.assertIsNone(auth.household_access(self.conn, friend, first))
        self.assertEqual(auth.household_access(self.conn, friend, second)[0], "owner")
        with self.assertRaises(ValueError):
            auth.redeem_invite(self.conn, token, "friend@example.test", "another-long-password-123", "Other")

    def test_session_revocation(self):
        uid, _ = auth.create_first_admin(self.conn, "owner@example.test", "very-long-password-123")
        token = auth.create_session(self.conn, uid)
        self.conn.commit()
        self.assertEqual(auth.session_user(self.conn, token)[0], uid)
        auth.revoke_session(self.conn, token)
        self.conn.commit()
        self.assertIsNone(auth.session_user(self.conn, token))

    def test_invite_requires_admin(self):
        uid, _ = auth.create_first_admin(self.conn, "owner@example.test", "very-long-password-123")
        token = auth.issue_invite(self.conn, uid, "friend@example.test")
        self.conn.commit()
        friend, _ = auth.redeem_invite(self.conn, token, "friend@example.test", "another-long-password-123", "Friend's Home")
        with self.assertRaises(PermissionError):
            auth.issue_invite(self.conn, friend, "other@example.test")

if __name__ == "__main__":
    unittest.main()
