import json
import sqlite3
import uuid

import pytest

from agentbox import store

T0 = "2026-10-25T12:00:00+00:00"


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "test.db")
    yield c
    c.close()


def add_action(conn, run_id, step=1, **overrides):
    fields = {
        "run_id": run_id,
        "step": step,
        "tool_name": "echo",
        "args": {"text": "hi"},
        "status": "ok",
        "started_at": T0,
        "duration_ms": 5,
        "result": {"text": "hi"},
    }
    fields.update(overrides)
    return store.save_action(conn, **fields)


def test_connect_creates_tables(conn):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    names = {row["name"] for row in rows}
    assert {"runs", "actions"} <= names


def test_start_run_returns_id(conn):
    run_id = store.start_run(conn, "first")
    other_id = store.start_run(conn, "second")

    uuid.UUID(run_id)  # raises if not a valid UUID
    assert run_id != other_id
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    assert row["name"] == "first"
    assert row["started_at"]


def test_save_action_returns_id(conn):
    run_id = store.start_run(conn, "r")
    action_id = add_action(conn, run_id, args={"a": [1, 2]})

    uuid.UUID(action_id)
    action = store.get_action(conn, action_id)
    assert json.loads(action["args_json"]) == {"a": [1, 2]}
    assert json.loads(action["result_json"]) == {"text": "hi"}


def test_save_action_with_error(conn):
    run_id = store.start_run(conn, "r")
    action_id = add_action(
        conn, run_id, result=store.NO_RESULT, error="boom", status="error"
    )

    action = store.get_action(conn, action_id)
    assert action["result_json"] is None
    assert action["has_result"] is False
    assert action["result"] is None
    assert action["error"] == "boom"
    assert action["status"] == "error"


@pytest.mark.parametrize(
    ("result", "stored"),
    [(None, "null"), (0, "0"), ("", '""')],
)
def test_save_action_falsy_results_are_kept(conn, result, stored):
    run_id = store.start_run(conn, "r")
    action_id = add_action(conn, run_id, result=result)

    action = store.get_action(conn, action_id)
    assert action["result_json"] == stored
    assert action["has_result"] is True
    assert action["result"] == result
    assert type(action["result"]) is type(result)


def test_none_zero_empty_and_error_told_apart(conn):
    run_id = store.start_run(conn, "r")
    add_action(conn, run_id, step=1, result=None)
    add_action(conn, run_id, step=2, result=0)
    add_action(conn, run_id, step=3, result="")
    add_action(conn, run_id, step=4, result=store.NO_RESULT, status="error")

    actions = store.list_actions(conn, run_id)
    seen = [(a["has_result"], a["result_json"]) for a in actions]
    assert seen == [(True, "null"), (True, "0"), (True, '""'), (False, None)]


def test_to_action_decodes_result():
    row = {"result_json": '{"x": 1}'}
    assert store.to_action(row) == {
        "result_json": '{"x": 1}',
        "has_result": True,
        "result": {"x": 1},
    }


def test_to_action_without_result():
    action = store.to_action({"result_json": None})
    assert action["has_result"] is False
    assert action["result"] is None


def test_save_action_unknown_run_fails(conn):
    with pytest.raises(sqlite3.IntegrityError):
        add_action(conn, "no-such-run")


def test_list_actions_ordered_and_scoped(conn):
    run_id = store.start_run(conn, "r")
    other_run = store.start_run(conn, "other")
    second = add_action(conn, run_id, step=2)
    first = add_action(conn, run_id, step=1)
    add_action(conn, other_run, step=1)

    actions = store.list_actions(conn, run_id)
    assert [a["id"] for a in actions] == [first, second]


def test_list_actions_empty(conn):
    run_id = store.start_run(conn, "r")
    assert store.list_actions(conn, run_id) == []


def test_get_action_found(conn):
    run_id = store.start_run(conn, "r")
    action_id = add_action(conn, run_id, step=3)

    assert store.get_action(conn, action_id) == {
        "id": action_id,
        "run_id": run_id,
        "step": 3,
        "tool_name": "echo",
        "args_json": '{"text": "hi"}',
        "result_json": '{"text": "hi"}',
        "error": None,
        "status": "ok",
        "started_at": T0,
        "duration_ms": 5,
        "rule_name": None,
        "has_result": True,
        "result": {"text": "hi"},
    }


def test_get_action_missing(conn):
    assert store.get_action(conn, "no-such-id") is None


def test_save_action_with_rule_name(conn):
    run_id = store.start_run(conn, "r")
    action_id = add_action(conn, run_id, status="blocked", rule_name="no-deletes")
    assert store.get_action(conn, action_id)["rule_name"] == "no-deletes"


def test_connect_adds_rule_name_to_old_database(tmp_path):
    db_path = tmp_path / "old.db"
    old = sqlite3.connect(db_path)
    old.executescript(
        """
        CREATE TABLE runs (id TEXT PRIMARY KEY, started_at TEXT NOT NULL,
                           name TEXT NOT NULL);
        CREATE TABLE actions (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
            step INTEGER NOT NULL, tool_name TEXT NOT NULL,
            args_json TEXT NOT NULL, result_json TEXT, error TEXT,
            status TEXT NOT NULL, started_at TEXT NOT NULL,
            duration_ms INTEGER NOT NULL
        );
        """
    )
    old.close()

    conn = store.connect(db_path)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(actions)")}
    assert "rule_name" in columns

    run_id = store.start_run(conn, "r")
    action_id = add_action(conn, run_id, rule_name="r1")
    assert store.get_action(conn, action_id)["rule_name"] == "r1"
    conn.close()
