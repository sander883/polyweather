"""Initialize the SQLite database from schema.sql.

Run with: python -m polyweather.db.init
"""

from pathlib import Path

from polyweather.db.connection import get_conn
from polyweather.logging_setup import setup_logging

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def init_db() -> None:
    sql = SCHEMA_PATH.read_text()
    with get_conn() as conn:
        conn.executescript(sql)


def main() -> None:
    setup_logging()
    init_db()
    print("DB initialized.")


if __name__ == "__main__":
    main()
