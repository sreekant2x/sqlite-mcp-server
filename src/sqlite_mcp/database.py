"""Read-only access to a SQLite file, safe to hand to a language model.

The model writes the SQL, so treat every query as untrusted input. Defence in depth:

1. The file is opened with ``mode=ro``: SQLite itself refuses to write.
2. ``PRAGMA query_only`` blocks writes even through tricks that reopen the file.
3. While a model-written query runs, an authorizer callback lets through only
   SELECT, reading columns and calling functions. ATTACH, PRAGMA, CREATE, INSERT,
   UPDATE, DELETE and everything else are denied before they execute.
4. sqlite3 refuses to execute more than one statement per call, so
   ``SELECT 1; DROP TABLE x`` fails outright.
5. A progress handler aborts queries that run longer than a deadline, and results are
   capped at a row limit, so a runaway CROSS JOIN cannot hang or flood the session.

String checks such as "starts with SELECT" are deliberately not used: the authorizer
sees the parsed statement, which a regex cannot.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

from .protocol import ToolError

DEFAULT_ROW_LIMIT = 100
MAX_ROW_LIMIT = 1000
DEFAULT_TIMEOUT_SECONDS = 5.0

_ALLOWED_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    getattr(sqlite3, "SQLITE_RECURSIVE", 33),  # WITH RECURSIVE; constant added in Python 3.11
}
_BLOCKED_FUNCTIONS = {"load_extension"}


def _authorizer(action: int, arg1: str | None, arg2: str | None, db_name: str | None, trigger: str | None) -> int:
    if action not in _ALLOWED_ACTIONS:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in _BLOCKED_FUNCTIONS:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _jsonable(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"blob_hex": value.hex()}
    return value


class Database:
    def __init__(self, path: str | Path, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS):
        self.path = Path(path)
        if not self.path.is_file():
            raise FileNotFoundError(f"SQLite database not found: {self.path}")
        self.timeout_seconds = timeout_seconds
        self.conn = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True)
        self.conn.execute("PRAGMA query_only = ON")
        # Installed once and switched on only around model-written SQL, because
        # set_authorizer(None) to remove it needs Python 3.11+.
        self._sandboxed = False
        self.conn.set_authorizer(self._authorize)

    def _authorize(self, *args: Any) -> int:
        return _authorizer(*args) if self._sandboxed else sqlite3.SQLITE_OK

    def close(self) -> None:
        self.conn.close()

    # -- schema introspection (trusted SQL written by this server) --------------------

    def list_tables(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        return [r[0] for r in rows]

    def describe_table(self, table: str) -> dict[str, Any]:
        # Identifiers cannot be bound as parameters, so only names that exist are allowed through.
        if table not in self.list_tables():
            raise LookupError(table)
        quoted = '"' + table.replace('"', '""') + '"'
        columns = [
            {
                "name": name,
                "type": col_type,
                "nullable": not notnull,
                "default": default,
                "primary_key": bool(pk),
            }
            for _, name, col_type, notnull, default, pk in self.conn.execute(f"PRAGMA table_info({quoted})")
        ]
        foreign_keys = [
            {"column": row[3], "references_table": row[2], "references_column": row[4]}
            for row in self.conn.execute(f"PRAGMA foreign_key_list({quoted})")
        ]
        row_count = self.conn.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
        return {"table": table, "row_count": row_count, "columns": columns, "foreign_keys": foreign_keys}

    def schema_sql(self) -> str:
        rows = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
            "ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'view' THEN 1 ELSE 2 END, name"
        ).fetchall()
        return ";\n\n".join(r[0] for r in rows) + ";\n"

    # -- untrusted, model-written SQL -------------------------------------------------

    def run_query(self, sql: str, limit: int = DEFAULT_ROW_LIMIT) -> dict[str, Any]:
        limit = max(1, min(limit, MAX_ROW_LIMIT))
        deadline = time.monotonic() + self.timeout_seconds

        self._sandboxed = True
        # Called every N SQLite VM instructions; a truthy return aborts the query.
        self.conn.set_progress_handler(lambda: time.monotonic() > deadline, 10_000)
        try:
            cursor = self.conn.execute(sql)
            if cursor.description is None:
                raise ToolError("Query returned no result set. Only SELECT queries are allowed.")
            columns = [d[0] for d in cursor.description]
            fetched = cursor.fetchmany(limit + 1)
        except sqlite3.DatabaseError as exc:  # includes ProgrammingError, e.g. two statements
            message = str(exc)
            if "not authorized" in message:
                message += ". This server is read-only: only SELECT queries are allowed."
            elif "interrupted" in message:
                message = f"Query took longer than {self.timeout_seconds:g}s and was stopped."
            raise ToolError(f"SQL error: {message}") from exc
        except sqlite3.Warning as exc:  # Python < 3.12 raised this for multiple statements
            raise ToolError(f"SQL error: {exc}") from exc
        finally:
            self._sandboxed = False
            self.conn.set_progress_handler(None, 0)

        truncated = len(fetched) > limit
        rows = [[_jsonable(v) for v in row] for row in fetched[:limit]]
        return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": truncated}
