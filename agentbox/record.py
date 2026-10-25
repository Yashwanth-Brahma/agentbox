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

logger = logging.getLogger("agentbox")

# Set only after the first warning about calls outside a run.
_warned = False


@dataclass
class _RunState:
    run_id: str
    conn: Connection
    step: int = 0


@dataclass
class _Call:
    state: _RunState
    step: int
    tool_name: str
    args: dict
    started_at: str
    start: float


# The run active in the current thread / async task, or None.
_current_run: ContextVar[_RunState | None] = ContextVar("current_run", default=None)


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
    """Decorator that saves each call of `fn` as an action in the current run."""
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(*args, **kwargs):
            call = _begin(fn, args, kwargs)
            if call is None:
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
        if call is None:
            return fn(*args, **kwargs)
        try:
            result = fn(*args, **kwargs)
        except Exception as e:
            _finish(call, error=e)
            raise
        _finish(call, result=result)
        return result

    return wrapper


def _begin(fn, args, kwargs) -> _Call | None:
    """Take the next step number and capture the args, or None outside a run."""
    global _warned
    state = _current_run.get()
    if state is None:
        if not _warned:
            logger.warning("%s called outside run(); not recording", fn.__name__)
            _warned = True
        return None

    # Bind to the signature so add(2, 3) and add(2, b=3) save the same args.
    bound = inspect.signature(fn).bind(*args, **kwargs)
    bound.apply_defaults()

    state.step += 1
    return _Call(
        state=state,
        step=state.step,
        tool_name=fn.__name__,
        args=dict(bound.arguments),
        started_at=datetime.now(UTC).isoformat(),
        start=time.perf_counter(),
    )


def _finish(call: _Call, result=store.NO_RESULT, error: Exception | None = None):
    """Save the finished call to the store."""
    duration_ms = round((time.perf_counter() - call.start) * 1000)
    store.save_action(
        call.state.conn,
        run_id=call.state.run_id,
        step=call.step,
        tool_name=call.tool_name,
        args=call.args,
        status="ok" if error is None else "error",
        started_at=call.started_at,
        duration_ms=duration_ms,
        result=result,
        error=None if error is None else f"{type(error).__name__}: {error}",
    )
