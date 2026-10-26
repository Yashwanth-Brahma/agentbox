import asyncio
import importlib
import logging
from pathlib import Path

import pytest

from agentbox import GuardBlocked, configure, record, run, store
from agentbox.rules import RulesError

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
def reset_module_state():
    record_module._warned = False
    yield
    configure(rules=None)


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


# --- rules connected to @record ---


@record
def write_file(path, text):
    Path(path).write_text(text)
    return "written"


@record
async def write_file_async(path, text):
    await asyncio.sleep(0)
    Path(path).write_text(text)
    return "written"


RULES = """
rules:
  - name: allow-notes
    tool: "write_*"
    when:
      path: { matches: "*notes.txt" }
    decision: allow
  - name: ask-secrets
    tool: "write_*"
    when:
      path: { matches: "*secret*" }
    decision: ask
  - name: no-writes
    tool: "write_*"
    decision: block
"""


@pytest.fixture
def use_rules(tmp_path):
    def _use(text=RULES):
        path = tmp_path / "rules.yaml"
        path.write_text(text)
        configure(rules=path)

    return _use


def test_no_rules_allows_everything(db_path, tmp_path):
    target = tmp_path / "out.txt"
    with run("r", db_path=db_path) as run_id:
        assert write_file(str(target), "hi") == "written"

    assert target.read_text() == "hi"
    [action] = actions_for(db_path, run_id)
    assert action["status"] == "ok"
    assert action["rule_name"] is None


def test_blocked_tool_does_not_run(db_path, tmp_path, use_rules):
    use_rules()
    target = tmp_path / "out.txt"
    with run("r", db_path=db_path) as run_id, pytest.raises(GuardBlocked) as caught:
        write_file(str(target), "hi")

    assert not target.exists()
    assert str(caught.value) == "write_file blocked by rule 'no-writes'"
    assert caught.value.decision == "block"
    assert caught.value.rule_name == "no-writes"
    [action] = actions_for(db_path, run_id)
    assert action["status"] == "blocked"
    assert action["rule_name"] == "no-writes"
    assert action["has_result"] is False
    assert action["error"] is None
    assert action["step"] == 1


def test_ask_is_saved_as_needs_approval(db_path, tmp_path, use_rules):
    use_rules()
    target = tmp_path / "secret.txt"
    with run("r", db_path=db_path) as run_id, pytest.raises(GuardBlocked) as caught:
        write_file(str(target), "hi")

    assert not target.exists()
    assert caught.value.decision == "ask"
    assert str(caught.value) == "write_file needs approval (rule 'ask-secrets')"
    [action] = actions_for(db_path, run_id)
    assert action["status"] == "needs_approval"
    assert action["rule_name"] == "ask-secrets"


def test_allowed_by_rule_runs_and_saves_rule_name(db_path, tmp_path, use_rules):
    use_rules()
    target = tmp_path / "notes.txt"
    with run("r", db_path=db_path) as run_id:
        assert write_file(str(target), "hi") == "written"

    assert target.read_text() == "hi"
    [action] = actions_for(db_path, run_id)
    assert action["status"] == "ok"
    assert action["rule_name"] == "allow-notes"
    assert action["result"] == "written"


def test_default_block(db_path, use_rules):
    use_rules("default: block\n")
    with run("r", db_path=db_path) as run_id, pytest.raises(GuardBlocked) as caught:
        add(1, 2)

    assert str(caught.value) == "add blocked by default"
    [action] = actions_for(db_path, run_id)
    assert action["status"] == "blocked"
    assert action["rule_name"] is None


def test_async_blocked_tool_does_not_run(db_path, tmp_path, use_rules):
    use_rules()
    target = tmp_path / "out.txt"
    with run("r", db_path=db_path) as run_id, pytest.raises(GuardBlocked):
        asyncio.run(write_file_async(str(target), "hi"))

    assert not target.exists()
    [action] = actions_for(db_path, run_id)
    assert action["status"] == "blocked"
    assert action["tool_name"] == "write_file_async"


def test_async_allowed_tool_runs(db_path, tmp_path, use_rules):
    use_rules()
    target = tmp_path / "notes.txt"
    with run("r", db_path=db_path) as run_id:
        assert asyncio.run(write_file_async(str(target), "hi")) == "written"

    assert target.read_text() == "hi"
    [action] = actions_for(db_path, run_id)
    assert action["status"] == "ok"


def test_blocked_outside_run_still_enforced(tmp_path, use_rules):
    use_rules()
    target = tmp_path / "out.txt"
    with pytest.raises(GuardBlocked):
        write_file(str(target), "hi")
    assert not target.exists()


def test_blocked_call_uses_a_step(db_path, tmp_path, use_rules):
    use_rules()
    with run("r", db_path=db_path) as run_id:
        with pytest.raises(GuardBlocked):
            write_file(str(tmp_path / "out.txt"), "hi")
        write_file(str(tmp_path / "notes.txt"), "hi")

    actions = actions_for(db_path, run_id)
    assert [(a["step"], a["status"]) for a in actions] == [(1, "blocked"), (2, "ok")]


def test_configure_none_allows_everything_again(tmp_path, use_rules):
    use_rules()
    configure(rules=None)
    target = tmp_path / "out.txt"
    write_file(str(target), "hi")
    assert target.exists()


def test_configure_bad_file_raises(tmp_path):
    path = tmp_path / "rules.yaml"
    path.write_text("default: maybe\n")
    with pytest.raises(RulesError):
        configure(rules=path)


def test_package_exports_guard():
    import agentbox

    assert agentbox.configure is configure
    assert agentbox.GuardBlocked is GuardBlocked


# --- chain rules connected to @record ---

sent = []  # emails the fake send_email actually sent


@record
def read_file(path):
    if path == "missing.env":
        raise FileNotFoundError(path)
    return f"contents of {path}"


@record
def send_email(to, body):
    sent.append((to, body))
    return "sent"


CHAIN_RULES = """
chains:
  - name: no-send-after-secrets
    after:
      tool: read_file
      when:
        path: { matches: "*.env*" }
    then:
      tool: "send_*"
      decision: block
"""


@pytest.fixture(autouse=True)
def clear_sent():
    sent.clear()


def test_env_read_then_send_is_blocked(db_path, use_rules):
    use_rules(CHAIN_RULES)
    with run("r", db_path=db_path) as run_id:
        read_file(".env")
        with pytest.raises(GuardBlocked) as caught:
            send_email("a@b.c", "secrets")

    assert sent == []
    assert caught.value.rule_name == "no-send-after-secrets"
    read, send = actions_for(db_path, run_id)
    assert (read["status"], read["rule_name"]) == ("ok", None)
    assert (send["status"], send["rule_name"]) == ("blocked", "no-send-after-secrets")


def test_send_without_read_is_allowed(db_path, use_rules):
    use_rules(CHAIN_RULES)
    with run("r", db_path=db_path):
        assert send_email("a@b.c", "hi") == "sent"
    assert sent == [("a@b.c", "hi")]


def test_other_file_read_does_not_fire(db_path, use_rules):
    use_rules(CHAIN_RULES)
    with run("r", db_path=db_path):
        read_file("notes.txt")
        assert send_email("a@b.c", "hi") == "sent"


def test_blocked_env_read_does_not_fire(db_path, use_rules):
    use_rules(
        CHAIN_RULES
        + """
rules:
  - name: no-env
    tool: read_file
    when:
      path: { matches: "*.env*" }
    decision: block
"""
    )
    with run("r", db_path=db_path) as run_id:
        with pytest.raises(GuardBlocked):
            read_file(".env")
        assert send_email("a@b.c", "hi") == "sent"

    read, send = actions_for(db_path, run_id)
    assert read["status"] == "blocked"
    assert send["status"] == "ok"


def test_failed_env_read_does_not_fire(db_path, use_rules):
    use_rules(CHAIN_RULES)
    with run("r", db_path=db_path):
        with pytest.raises(FileNotFoundError):
            read_file("missing.env")
        assert send_email("a@b.c", "hi") == "sent"


def test_runs_do_not_share_fired_chains(db_path, use_rules):
    use_rules(CHAIN_RULES)
    with run("first", db_path=db_path):
        read_file(".env")
        with pytest.raises(GuardBlocked):
            send_email("a@b.c", "x")
    with run("second", db_path=db_path):
        assert send_email("a@b.c", "hi") == "sent"
