"""Try @record by hand: call 3 fake tools inside run("demo"), print the actions.

Run with: uv run python scripts/try_record.py
"""

from agentbox import record, run, store


@record
def add(a, b):
    return a + b


@record
def greet(name):
    return f"hello {name}"


@record
def divide(a, b):
    return a / b


with run("demo") as run_id:
    add(2, 3)
    greet("agentbox")
    try:
        divide(1, 0)
    except ZeroDivisionError as e:
        print(f"caught: {e}")

conn = store.connect("agentbox.db")
print(f"run {run_id}")
for action in store.list_actions(conn, run_id):
    print(
        f"  step {action['step']} {action['tool_name']}({action['args_json']}):"
        f" {action['status']} result={action['result']!r}"
        f" error={action['error']!r} {action['duration_ms']}ms"
    )
conn.close()
