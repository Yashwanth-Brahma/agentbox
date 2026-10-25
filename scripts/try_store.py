"""Try the store by hand: start a run, save 2 fake actions, print them.

Run with: uv run python scripts/try_store.py
"""

from datetime import UTC, datetime

from agentbox import store

conn = store.connect("agentbox.db")
run_id = store.start_run(conn, "try-store")

now = datetime.now(UTC).isoformat()
store.save_action(
    conn,
    run_id=run_id,
    step=1,
    tool_name="echo",
    args={"text": "hello"},
    status="ok",
    started_at=now,
    duration_ms=3,
    result={"text": "hello"},
)
store.save_action(
    conn,
    run_id=run_id,
    step=2,
    tool_name="read_file",
    args={"path": "missing.txt"},
    status="error",
    started_at=now,
    duration_ms=7,
    error="file not found",
)

print(f"run {run_id}")
for action in store.list_actions(conn, run_id):
    print(
        f"  step {action['step']} {action['tool_name']}: {action['status']}"
        f" has_result={action['has_result']} result={action['result']!r}"
        f" error={action['error']!r}"
    )
conn.close()
