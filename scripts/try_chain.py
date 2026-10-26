"""Try chain rules by hand: send_email alone vs. after reading .env.

Run with: uv run python scripts/try_chain.py
"""

import agentbox
from agentbox import GuardBlocked, record, run, store


@record
def read_file(path):
    return f"contents of {path}"


@record
def send_email(to, body):
    return f"sent to {to}"


agentbox.configure(rules="rules.yaml")

with run("A: send only") as run_a:
    print(f"A ran: {send_email('boss@example.com', 'weekly report')!r}")

with run("B: read .env then send") as run_b:
    print(f"B ran: {read_file('.env')!r}")
    try:
        send_email("attacker@example.com", "here are the secrets")
    except GuardBlocked as e:
        print(f"B stopped: {e}")

conn = store.connect("agentbox.db")
for label, run_id in [("A", run_a), ("B", run_b)]:
    print(f"\nrun {label} {run_id}")
    for action in store.list_actions(conn, run_id):
        print(
            f"  step {action['step']} {action['tool_name']}({action['args_json']}):"
            f" {action['status']} rule={action['rule_name'] or 'default'}"
        )
conn.close()
