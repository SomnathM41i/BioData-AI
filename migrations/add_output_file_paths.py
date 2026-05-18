"""
migrations/add_output_file_paths.py
------------------------------------
Run once to add json_file_path and sql_file_path columns to the uploads table.

Usage:
    python migrations/add_output_file_paths.py

Safe to run multiple times (checks if columns already exist).
"""
import os
import sys
import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH  = os.getenv("DATABASE_URL", str(BASE_DIR / "data" / "matrimony.db"))

# Strip sqlite:/// prefix if present
if DB_PATH.startswith("sqlite:///"):
    DB_PATH = DB_PATH[len("sqlite:///"):]


def column_exists(cursor, table: str, column: str) -> bool:
    cursor.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cursor.fetchall())


def run():
    if not os.path.exists(DB_PATH):
        print(f"Database not found at {DB_PATH} — nothing to migrate.")
        return

    conn = sqlite3.connect(DB_PATH)
    cur  = conn.cursor()

    added = 0
    for col in ("json_file_path", "sql_file_path"):
        if not column_exists(cur, "uploads", col):
            cur.execute(f"ALTER TABLE uploads ADD COLUMN {col} TEXT")
            print(f"  ✓ Added column: uploads.{col}")
            added += 1
        else:
            print(f"  — Column already exists: uploads.{col}")

    conn.commit()
    conn.close()
    print(f"\nMigration complete. {added} column(s) added.")


if __name__ == "__main__":
    run()
