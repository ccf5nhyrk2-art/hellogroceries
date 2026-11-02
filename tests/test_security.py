import hashlib
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import households as auth
import household_sharing as sharing


class SecurityRegressionTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.execute('PRAGMA foreign_keys=ON')
        auth.ensure_schema(self.conn)
        sharing.ensure_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_password_hash_salted_and_not_plaintext(self):
        password = 'a sufficiently long test password'
        a = auth.password_hash(password)
        b = auth.password_hash(password)
        self.assertNotEqual(a, b)
        self.assertNotIn(password, a)
        self.assertTrue(auth.verify_password(password, a))
        self.assertFalse(auth.verify_password('wrong password', a))

    def test_random_session_token_is_hashed(self):
        self.assertTrue(callable(auth.token_hash))
        token = 'a-random-example-session-token'
        self.assertEqual(auth.token_hash(token), hashlib.sha256(token.encode()).hexdigest())
        self.assertNotEqual(auth.token_hash(token), token)


if __name__ == '__main__':
    unittest.main()
