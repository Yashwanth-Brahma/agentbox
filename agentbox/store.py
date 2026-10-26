"""SQLite store for agent runs and the actions (tool calls) inside them."""

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actions (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    step INTEGER NOT NULL,
    tool_name TEXT NOT NULL,
    args_json TEXT NOT NULL,
    result_json TEXT,
    error TEXT,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    rule_name TEXT
);
"""

# Default for save_action's result: "the tool produced no result" (error, blocked,
# not run). Needed because None is a real result a tool can return.
NO_RESULT = object()


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open the database and create tables if they don't exist."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # SQLite ignores REFERENCES unless this is turned on for each connection.
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _add_missing_columns(conn)
    return conn


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """Upgrade databases created before a column existed."""
    # CREATE TABLE IF NOT EXISTS skips existing tables, so new columns need ALTER.
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(actions)")}
    if "rule_name" not in columns:
        with conn:
            conn.execute("ALTER TABLE actions ADD COLUMN rule_name TEXT")


def start_run(conn: sqlite3.Connection, name: str) -> str:
    """Create a new run and return its id."""
    run_id = str(uuid.uuid4())
    started_at = datetime.now(UTC).isoformat()
    with conn:
        conn.execute(
            "INSERT INTO runs (id, started_at, name) VALUES (?, ?, ?)",
            (run_id, started_at, name),
        )
    return run_id


def save_action(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    step: int,
    tool_name: str,
    args: object,
    status: str,
    started_at: str,
    duration_ms: int,
    result: object = NO_RESULT,
    error: str | None = None,
    rule_name: str | None = None,
) -> str:
    """Save one tool call for a run and return its id.

    Leave out `result` when there is none; a returned None is saved as "null".
    """
    action_id = str(uuid.uuid4())
    result_json = None if result is NO_RESULT else json.dumps(result)
    with conn:
        conn.execute(
            """
            INSERT INTO actions (id, run_id, step, tool_name, args_json, result_json,
                                 error, status, started_at, duration_ms, rule_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                action_id,
                run_id,
                step,
                tool_name,
                json.dumps(args),
                result_json,
                error,
                status,
                started_at,
                duration_ms,
                rule_name,
            ),
        )
    return action_id


def list_actions(conn: sqlite3.Connection, run_id: str) -> list[dict]:
    """Return all actions for a run, in step order."""
    rows = conn.execute(
        "SELECT * FROM actions WHERE run_id = ? ORDER BY step, started_at",
        (run_id,),
    ).fetchall()
    return [to_action(dict(row)) for row in rows]


def get_action(conn: sqlite3.Connection, action_id: str) -> dict | None:
    """Return one action by id, or None if it doesn't exist."""
    row = conn.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
    return to_action(dict(row)) if row else None


def to_action(row: dict) -> dict:
    """Add decoded `result` and `has_result` to a raw actions row."""
    # SQL NULL means no result; the text "null" means the tool returned None.
    has_result = row["result_json"] is not None
    row["has_result"] = has_result
    row["result"] = json.loads(row["result_json"]) if has_result else None
    return row
