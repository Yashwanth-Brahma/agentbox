"""Record tool calls automatically: `with run(name):` plus `@record` on tools."""

import functools
import inspect
import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from sqlite3 import Connection

from agentbox import store
from agentbox.rules import Rules, load_rules

logger = logging.getLogger("agentbox")

# Set only after the first warning about calls outside a run.
_warned = False

# Rules loaded by configure(); None means allow everything.
_rules: Rules | None = None

# Decision -> status saved for a call that was stopped before running.
_STOPPED_STATUS = {"block": "blocked", "ask": "needs_approval"}


class GuardBlocked(Exception):
    """A tool call was stopped by the rules (blocked, or needs approval)."""

    def __init__(self, tool_name: str, decision: str, rule_name: str | None):
        self.tool_name = tool_name
        self.decision = decision
        self.rule_name = rule_name
        by = f"rule {rule_name!r}" if rule_name else "default"
        if decision == "ask":
            message = f"{tool_name} needs approval ({by})"
        else:
            message = f"{tool_name} blocked by {by}"
        super().__init__(message)


@dataclass
class _RunState:
    run_id: str
    conn: Connection
    step: int = 0


@dataclass
class _Call:
    state: _RunState | None  # None outside a run: checked by rules, not saved
    step: int | None
    tool_name: str
    args: dict
    started_at: str
    start: float
    rule_name: str | None = None


# The run active in the current thread / async task, or None.
_current_run: ContextVar[_RunState | None] = ContextVar("current_run", default=None)


def configure(rules: str | Path | None = None) -> None:
    """Load the rules file once. None (the default) means allow everything."""
    global _rules
    _rules = None if rules is None else load_rules(rules)


@contextmanager
def run(name: str, db_path: str | Path = "agentbox.db"):
    """Start a run; tool calls made inside it are saved. Yields the run id."""
    conn = store.connect(db_path)
    try:
        state = _RunState(run_id=store.start_run(conn, name), conn=conn)
        token = _current_run.set(state)
        try:
            yield state.run_id
        finally:
            # Restores whatever run (if any) was active before this one.
            _current_run.reset(token)
    finally:
        conn.close()


def record(fn):
    """Decorator that checks the rules, then saves each call of `fn` in the run."""
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(*args, **kwargs):
            call = _begin(fn, args, kwargs)
            _guard(call)  # raises GuardBlocked before the tool runs
            if call.state is None:
                return await fn(*args, **kwargs)
            try:
                result = await fn(*args, **kwargs)
            except Exception as e:
                _finish(call, error=e)
                raise
            _finish(call, result=result)
            return result

        return async_wrapper

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        call = _begin(fn, args, kwargs)
        _guard(call)  # raises GuardBlocked before the tool runs
        if call.state is None:
            return fn(*args, **kwargs)
        try:
            result = fn(*args, **kwargs)
        except Exception as e:
            _finish(call, error=e)
            raise
        _finish(call, result=result)
        return result

    return wrapper


def _begin(fn, args, kwargs) -> _Call:
    """Capture the args, and take the next step number if a run is active."""
    global _warned
    state = _current_run.get()
    if state is None and not _warned:
        logger.warning("%s called outside run(); not recording", fn.__name__)
        _warned = True

    # Bind to the signature so add(2, 3) and add(2, b=3) save the same args.
    bound = inspect.signature(fn).bind(*args, **kwargs)
    bound.apply_defaults()

    step = None
    if state is not None:
        state.step += 1
        step = state.step
    return _Call(
        state=state,
        step=step,
        tool_name=fn.__name__,
        args=dict(bound.arguments),
        started_at=datetime.now(UTC).isoformat(),
        start=time.perf_counter(),
    )


def _guard(call: _Call) -> None:
    """Check the rules. If not allowed, save the attempt and raise GuardBlocked."""
    if _rules is None:
        return
    decision = _rules.decide(call.tool_name, call.args)
    call.rule_name = decision.rule_name
    if decision.decision == "allow":
        return
    # Rules apply even outside a run; only the saving needs a run.
    if call.state is not None:
        _save(call, status=_STOPPED_STATUS[decision.decision], duration_ms=0)
    raise GuardBlocked(call.tool_name, decision.decision, decision.rule_name)


def _finish(call: _Call, result=store.NO_RESULT, error: Exception | None = None):
    """Save the finished call to the store."""
    duration_ms = round((time.perf_counter() - call.start) * 1000)
    _save(
        call,
        status="ok" if error is None else "error",
        duration_ms=duration_ms,
        result=result,
        error=None if error is None else f"{type(error).__name__}: {error}",
    )


def _save(call: _Call, *, status: str, duration_ms: int, **extra) -> None:
    store.save_action(
        call.state.conn,
        run_id=call.state.run_id,
        step=call.step,
        tool_name=call.tool_name,
        args=call.args,
        status=status,
        started_at=call.started_at,
        duration_ms=duration_ms,
        rule_name=call.rule_name,
        **extra,
    )
