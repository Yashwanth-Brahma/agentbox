import asyncio
import importlib
import logging

import pytest

from agentbox import record, run, store

# `agentbox.record` is shadowed by the decorator in __init__, so fetch the module.
record_module = importlib.import_module("agentbox.record")


@record
def add(a, b=10):
    return a + b


@record
def fail():
    raise ValueError("boom")


@record
async def add_async(a, b):
    await asyncio.sleep(0)
    return a + b


@record
async def fail_async():
    await asyncio.sleep(0)
    raise ValueError("async boom")


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "test.db"


@pytest.fixture(autouse=True)
def reset_warning():
    record_module._warned = False


def actions_for(db_path, run_id):
    conn = store.connect(db_path)
    try:
        return store.list_actions(conn, run_id)
    finally:
        conn.close()


def test_run_yields_run_id_and_saves_run(db_path):
    with run("my-run", db_path=db_path) as run_id:
        pass

    conn = store.connect(db_path)
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    conn.close()
    assert row["name"] == "my-run"


def test_record_saves_sync_call(db_path):
    with run("r", db_path=db_path) as run_id:
        assert add(2, 3) == 5

    [action] = actions_for(db_path, run_id)
    assert action["tool_name"] == "add"
    assert action["args_json"] == '{"a": 2, "b": 3}'
    assert action["result"] == 5
    assert action["status"] == "ok"
    assert action["run_id"] == run_id
    assert action["step"] == 1
    assert isinstance(action["duration_ms"], int)
    assert action["duration_ms"] >= 0


def test_record_binds_kwargs_and_defaults(db_path):
    with run("r", db_path=db_path) as run_id:
        add(2, b=3)
        add(a=1)

    first, second = actions_for(db_path, run_id)
    assert first["args_json"] == '{"a": 2, "b": 3}'
    assert second["args_json"] == '{"a": 1, "b": 10}'


def test_steps_increase_and_reset_per_run(db_path):
    with run("first", db_path=db_path) as first_run:
        add(1, 1)
        add(2, 2)
    with run("second", db_path=db_path) as second_run:
        add(3, 3)

    assert [a["step"] for a in actions_for(db_path, first_run)] == [1, 2]
    assert [a["step"] for a in actions_for(db_path, second_run)] == [1]


def test_record_failing_tool_saves_error_and_reraises(db_path):
    with run("r", db_path=db_path) as run_id, pytest.raises(ValueError, match="boom"):
        fail()

    [action] = actions_for(db_path, run_id)
    assert action["status"] == "error"
    assert action["error"] == "ValueError: boom"
    assert action["has_result"] is False


def test_record_reraises_same_exception_object(db_path):
    err = RuntimeError("original")

    @record
    def raise_it():
        raise err

    with run("r", db_path=db_path), pytest.raises(RuntimeError) as caught:
        raise_it()
    assert caught.value is err


def test_record_async_tool(db_path):
    with run("r", db_path=db_path) as run_id:
        assert asyncio.run(add_async(4, 5)) == 9

    [action] = actions_for(db_path, run_id)
    assert action["tool_name"] == "add_async"
    assert action["args_json"] == '{"a": 4, "b": 5}'
    assert action["result"] == 9
    assert action["status"] == "ok"
    assert action["step"] == 1


def test_record_async_failing_tool(db_path):
    with (
        run("r", db_path=db_path) as run_id,
        pytest.raises(ValueError, match="async boom"),
    ):
        asyncio.run(fail_async())

    [action] = actions_for(db_path, run_id)
    assert action["status"] == "error"
    assert action["error"] == "ValueError: async boom"


def test_record_outside_run(caplog):
    with caplog.at_level(logging.WARNING, logger="agentbox"):
        assert add(1, 2) == 3
        assert add(3, 4) == 7

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "outside run()" in warnings[0].getMessage()


def test_package_exports():
    import agentbox

    assert agentbox.run is run
    assert agentbox.record is record
