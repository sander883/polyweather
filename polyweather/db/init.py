"""Initialize / migrate the SQLite database.

Run with: python -m polyweather.db.init

The schema file uses ``CREATE TABLE IF NOT EXISTS``, so a fresh file is safe.
For existing DBs we also run small idempotent migrations that add new columns
introduced after v1 (SQLite has no ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS``
so we check ``PRAGMA table_info`` first).
"""

from pathlib import Path

from polyweather.db.connection import get_conn
from polyweather.logging_setup import setup_logging

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def _columns(conn, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column_if_missing(conn, table: str, column: str, ddl: str) -> None:
    if column not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _run_migrations(conn) -> None:
    # v1.1 — distinguish temperature-high vs temperature-low markets
    _add_column_if_missing(conn, "markets", "market_type", "TEXT")


def init_db() -> None:
    sql = SCHEMA_PATH.read_text()
    with get_conn() as conn:
        conn.executescript(sql)
        _run_migrations(conn)


def main() -> None:
    setup_logging()
    init_db()
    print("DB initialized.")


if __name__ == "__main__":
    main()
