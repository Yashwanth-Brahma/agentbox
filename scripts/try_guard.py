"""Try the guard by hand: rules.yaml + @record inside a run, then print the actions.

Run with: uv run python scripts/try_guard.py
"""

import agentbox
from agentbox import GuardBlocked, record, run, store


@record
def get_user(id):
    return {"id": id, "name": "Ada"}


@record
def delete_user(id):
    return f"deleted {id}"


@record
def refund(amount):
    return f"refunded {amount}"


agentbox.configure(rules="rules.yaml")

calls = [
    (get_user, {"id": 1}),
    (delete_user, {"id": 5}),
    (refund, {"amount": 8000}),
    (refund, {"amount": 200}),
]

with run("guard-demo") as run_id:
    for tool, kwargs in calls:
        try:
            print(f"ran: {tool(**kwargs)!r}")
        except GuardBlocked as e:
            print(f"stopped: {e}")

conn = store.connect("agentbox.db")
print(f"\nrun {run_id}")
for action in store.list_actions(conn, run_id):
    print(
        f"  step {action['step']} {action['tool_name']}({action['args_json']}):"
        f" {action['status']} rule={action['rule_name'] or 'default'}"
    )
conn.close()
