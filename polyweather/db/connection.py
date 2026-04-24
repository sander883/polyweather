import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from polyweather.config import get_settings


def _ensure_parent(path: str) -> None:
    parent = Path(path).parent
    parent.mkdir(parents=True, exist_ok=True)


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or get_settings().db_path
    _ensure_parent(path)
    conn = sqlite3.connect(path, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def get_conn(db_path: str | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
