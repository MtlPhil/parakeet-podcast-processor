"""The DuckDB connection: one cursor per thread, transactions, app state."""

import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import duckdb

from .. import paths


class Connection:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = Path(db_path) if db_path else paths.database()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._base_conn = duckdb.connect(str(self.db_path))
        self._local = threading.local()

    @property
    def conn(self):
        """Per-thread cursor over the shared DuckDB connection.

        A single DuckDB connection object is not safe for concurrent use. Under
        FastAPI, sync endpoints run in threadpool workers, so each thread gets
        its own cursor (sharing the same underlying database) instead.
        """
        if self._base_conn is None:
            return None
        cursor = getattr(self._local, "cursor", None)
        if cursor is None:
            cursor = self._base_conn.cursor()
            self._local.cursor = cursor
        return cursor

    @contextmanager
    def transaction(self):
        """Make the statements run inside the block all happen or none.

        Per thread, like ``conn``. A block nested in another joins the outer
        one. Not for deleting a row together with the rows that reference
        it: DuckDB checks foreign keys against the state before the
        transaction, so the parent's delete is refused (see delete_podcast).
        """
        depth = getattr(self._local, "transaction_depth", 0)
        if depth == 0:
            self.conn.execute("BEGIN")
        self._local.transaction_depth = depth + 1
        try:
            yield
        except BaseException:
            if depth == 0:
                self.conn.execute("ROLLBACK")
            raise
        else:
            if depth == 0:
                self.conn.execute("COMMIT")
        finally:
            self._local.transaction_depth = depth

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def _row_to_dict(self, row, description) -> Dict[str, Any]:
        """Map a result row to a dict using cursor column names."""
        return {desc[0]: val for desc, val in zip(description, row)}

    def _fetchall_as_dicts(self, cursor) -> List[Dict[str, Any]]:
        """Fetch all rows from a cursor as a list of dicts."""
        description = cursor.description
        return [self._row_to_dict(row, description) for row in cursor.fetchall()]

    def _fetchone_as_dict(self, cursor) -> Optional[Dict[str, Any]]:
        """Fetch one row from a cursor as a dict, or None."""
        row = cursor.fetchone()
        if row is None:
            return None
        return self._row_to_dict(row, cursor.description)

    def now(self) -> datetime:
        """The database clock, as its CURRENT_TIMESTAMP defaults record it."""
        return self.conn.execute("SELECT CURRENT_TIMESTAMP::TIMESTAMP").fetchone()[0]

    def get_state(self, key: str) -> Optional[str]:
        """Get a stored app-state value, or None if unset."""
        row = self.conn.execute(
            "SELECT value FROM app_state WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None

    def set_state(self, key: str, value: str):
        """Store an app-state value, replacing any previous one."""
        self.conn.execute(
            "INSERT OR REPLACE INTO app_state (key, value) VALUES (?, ?)",
            (key, value),
        )

    def close(self):
        """Close database connection."""
        if self._base_conn is not None:
            self._base_conn.close()
            self._base_conn = None
            self._local = threading.local()
