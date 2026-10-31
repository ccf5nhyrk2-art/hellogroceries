import sqlite3
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from backup_sqlite import backup

class BackupTests(unittest.TestCase):
    def test_backup_is_readable_and_verified(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            source = base / "data" / "shopping.db"
            source.parent.mkdir()
            with sqlite3.connect(source) as db:
                db.execute("CREATE TABLE groceries (name TEXT)")
                db.execute("INSERT INTO groceries VALUES ('Apples')")
            result = backup(source, base / "backups")
            self.assertTrue(result.exists())
            with sqlite3.connect(result) as db:
                self.assertEqual(db.execute("SELECT name FROM groceries").fetchone()[0], "Apples")
                self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_refuses_same_database_directory(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "shopping.db"
            with sqlite3.connect(source) as db:
                db.execute("CREATE TABLE groceries (name TEXT)")
            with self.assertRaises(ValueError):
                backup(source, Path(root))

if __name__ == "__main__":
    unittest.main()
