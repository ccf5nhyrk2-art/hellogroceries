#!/usr/bin/env python3
"""Consistent, verified SQLite backup of HelloGroceries data.

Run on the OMV host with the source database and destination mounted.
Example:
  python3 scripts/backup_sqlite.py /path/to/data/shopping.db /path/to/backups
The backup destination should be outside the app's Docker volume.
"""
import argparse
import datetime
import os
from pathlib import Path
import sqlite3
import sys

def backup(source: Path, destination: Path, keep: int = 14) -> Path:
    source = source.resolve(strict=True)
    if not source.is_file():
        raise ValueError("Source must be a SQLite database file")
    destination.mkdir(parents=True, exist_ok=True)
    destination = destination.resolve(strict=True)
    if destination == source.parent:
        raise ValueError("Backup destination must differ from database directory")
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = destination / ("hellogroceries-" + stamp + ".sqlite3")
    temp = destination / ("." + target.name + ".partial")
    try:
        with sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30) as original:
            with sqlite3.connect(str(temp), timeout=30) as copy:
                original.backup(copy)
                result = copy.execute("PRAGMA integrity_check").fetchone()[0]
                if result != "ok":
                    raise RuntimeError("Backup integrity check failed: " + result)
        os.chmod(temp, 0o600)
        temp.replace(target)
    finally:
        temp.unlink(missing_ok=True)
    backups = sorted(destination.glob("hellogroceries-*.sqlite3"), reverse=True)
    for old in backups[max(keep, 1):]:
        old.unlink()
    return target

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("backup_directory", type=Path)
    parser.add_argument("--keep", type=int, default=14)
    args = parser.parse_args()
    if args.keep < 1:
        parser.error("--keep must be at least 1")
    try:
        print("Verified backup:", backup(args.database, args.backup_directory, args.keep))
    except (OSError, ValueError, sqlite3.Error, RuntimeError) as exc:
        print("Backup failed:", exc, file=sys.stderr)
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
