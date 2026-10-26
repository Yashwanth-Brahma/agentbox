"""Try the rules engine by hand: load rules.yaml, print decisions for a few calls.

Run with: uv run python scripts/try_rules.py
"""

from agentbox.rules import load_rules

rules = load_rules("rules.yaml")

calls = [
    ("delete_user", {"id": 5}),
    ("refund", {"amount": 8000}),
    ("refund", {"amount": 200}),
    ("run_sql", {"query": "drop table orders"}),
    ("get_user", {"id": 1}),
]

for tool_name, args in calls:
    d = rules.decide(tool_name, args)
    arg_text = ", ".join(f"{k}={v!r}" for k, v in args.items())
    print(f"{tool_name}({arg_text}) -> {d.decision} (rule: {d.rule_name or 'default'})")
