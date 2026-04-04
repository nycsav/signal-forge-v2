#!/usr/bin/env python3
"""Initialize the Signal Forge v2 database."""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "trades.db"
SCHEMA_PATH = Path(__file__).parent.parent / "db" / "schema.sql"


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    schema = SCHEMA_PATH.read_text()
    conn.executescript(schema)
    conn.commit()

    # Verify tables
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    table_names = [t[0] for t in tables]
    conn.close()

    print(f"Database initialized at {DB_PATH} with {len(table_names)} tables")
    for t in table_names:
        print(f"  - {t}")


if __name__ == "__main__":
    init_db()
