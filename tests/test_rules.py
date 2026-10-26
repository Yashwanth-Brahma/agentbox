from pathlib import Path

import pytest

from agentbox.rules import (
    Decision,
    RulesError,
    _call_matches,
    _is_number,
    _label,
    _to_number,
    load_rules,
)

EXAMPLE = Path(__file__).parent.parent / "rules.yaml"


@pytest.fixture
def load(tmp_path):
    def _load(text):
        path = tmp_path / "rules.yaml"
        path.write_text(text)
        return load_rules(path)

    return _load


def one_rule(when):
    """YAML for a single rule on tool 't' that blocks when `when` holds."""
    return f"""
rules:
  - name: r
    tool: t
    when:
      {when}
    decision: block
"""


@pytest.mark.parametrize(
    ("when", "arg", "expected"),
    [
        ("x: { eq: 5 }", 5, True),
        ("x: { eq: 5 }", 6, False),
        ("x: { ne: 5 }", 6, True),
        ("x: { ne: 5 }", 5, False),
        ("x: { gt: 5 }", 6, True),
        ("x: { gt: 5 }", 5, False),
        ("x: { gte: 5 }", 5, True),
        ("x: { gte: 5 }", 4, False),
        ("x: { lt: 5 }", 4, True),
        ("x: { lt: 5 }", 5, False),
        ("x: { lte: 5 }", 5, True),
        ("x: { lte: 5 }", 6, False),
        ("x: { contains: 'etc' }", "/etc/passwd", True),
        ("x: { contains: 'etc' }", "/home/me", False),
        ("x: { contains: 'b' }", ["a", "b"], True),
        ("x: { contains: 'z' }", ["a", "b"], False),
        ("x: { matches: '*DROP*' }", "drop table users", True),
        ("x: { matches: '*DROP*' }", "select 1", False),
    ],
)
def test_each_condition(load, when, arg, expected):
    rules = load(one_rule(when))
    blocked = rules.decide("t", {"x": arg}) == Decision("block", "r")
    assert blocked is expected


@pytest.mark.parametrize(
    ("when", "arg"),
    [
        ("x: { gt: 5 }", "abc"),
        ("x: { contains: 'a' }", 123),
        ("x: { matches: '*1*' }", 123),
    ],
)
def test_incomparable_values_do_not_match(load, when, arg):
    rules = load(one_rule(when))
    assert rules.decide("t", {"x": arg}) == Decision("allow", None)


def test_tool_glob(load):
    rules = load("rules:\n  - {name: r, tool: 'delete_*', decision: block}\n")
    assert rules.decide("delete_file", {}) == Decision("block", "r")
    assert rules.decide("read_file", {}) == Decision("allow", None)


def test_all_conditions_must_hold(load):
    rules = load(one_rule("x: { gt: 5 }\n      y: { eq: 'yes' }"))
    assert rules.decide("t", {"x": 6, "y": "yes"}).decision == "block"
    assert rules.decide("t", {"x": 6, "y": "no"}).decision == "allow"


def test_first_match_wins(load):
    rules = load(
        """
rules:
  - {name: first, tool: 'refund', decision: ask}
  - {name: second, tool: '*', decision: block}
"""
    )
    assert rules.decide("refund", {}) == Decision("ask", "first")
    assert rules.decide("other", {}) == Decision("block", "second")


def test_default_is_allow_when_missing(load):
    rules = load("rules: []\n")
    assert rules.decide("anything", {}) == Decision("allow", None)


def test_default_is_used_when_nothing_matches(load):
    rules = load("default: block\nrules:\n  - {name: r, tool: x, decision: allow}\n")
    assert rules.decide("y", {}) == Decision("block", None)


def test_empty_file_allows_everything(load):
    assert load("").decide("anything", {}) == Decision("allow", None)


def test_missing_argument_does_not_match(load):
    rules = load(one_rule("amount: { gt: 5000 }"))
    assert rules.decide("t", {}) == Decision("allow", None)
    assert rules.decide("t", {"other": 9999}) == Decision("allow", None)


def test_bad_yaml(load):
    with pytest.raises(RulesError, match="invalid YAML"):
        load("rules: [unclosed\n")


def test_unknown_condition(load):
    with pytest.raises(
        RulesError, match="rule 'r' \\(#1\\): unknown condition 'equals'"
    ):
        load(one_rule("x: { equals: 5 }"))


def test_unknown_decision(load):
    with pytest.raises(RulesError, match="decision must be one of"):
        load("rules:\n  - {name: r, tool: x, decision: maybe}\n")


def test_bad_default(load):
    with pytest.raises(RulesError, match="default must be one of"):
        load("default: deny\n")


def test_condition_with_two_keys(load):
    with pytest.raises(RulesError, match="exactly one key"):
        load(one_rule("x: { gt: 1, lt: 9 }"))


def test_unknown_rule_key(load):
    with pytest.raises(RulesError, match="unknown key"):
        load("rules:\n  - {name: r, tool: x, whn: {}, decision: block}\n")


def test_not_a_mapping(load):
    with pytest.raises(RulesError, match="must be a mapping"):
        load("- just\n- a list\n")


def test_example_rules_file():
    rules = load_rules(EXAMPLE)
    assert rules.decide("delete_user", {}) == Decision("block", "no-deletes")
    assert rules.decide("refund", {"amount": 6000}) == Decision("ask", "big-refunds")
    assert rules.decide("refund", {"amount": 100}) == Decision("allow", None)
    assert rules.decide("run_sql", {"query": "drop table x"}) == Decision(
        "block", "readonly-sql"
    )
    assert rules.decide("read_file", {"path": "a.txt"}) == Decision("allow", None)


@pytest.mark.parametrize(
    ("when", "arg", "expected"),
    [
        ("x: { gt: 5000 }", "8000", True),
        ("x: { gt: 5000 }", "200", False),
        ("x: { gte: 5000 }", " 5000 ", True),
        ("x: { lt: 5000 }", "12.5", True),
        ("x: { lte: 5000 }", "1e4", False),
        ("x: { gt: 5000 }", "nan", False),
        ("x: { lt: 5000 }", "NaN", False),
        ("x: { gt: 5000 }", "8,000", False),
        ("x: { gt: 5000 }", "", False),
    ],
)
def test_number_strings_are_converted(load, when, arg, expected):
    rules = load(one_rule(when))
    blocked = rules.decide("t", {"x": arg}) == Decision("block", "r")
    assert blocked is expected


def test_string_amount_in_example_rules():
    rules = load_rules(EXAMPLE)
    assert rules.decide("refund", {"amount": "8000"}) == Decision("ask", "big-refunds")
    assert rules.decide("refund", {"amount": "200"}) == Decision("allow", None)


def test_eq_does_not_convert_strings(load):
    rules = load(one_rule("x: { eq: 5 }"))
    assert rules.decide("t", {"x": "5"}).decision == "allow"


@pytest.mark.parametrize(
    ("text", "expected"),
    [("8000", 8000.0), (" 12.5 ", 12.5), ("-3", -3.0), ("1e3", 1000.0)],
)
def test_to_number_parses(text, expected):
    assert _to_number(text) == expected


@pytest.mark.parametrize("text", ["nan", "NaN", "abc", "", "8,000"])
def test_to_number_rejects(text):
    assert _to_number(text) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [(5, True), (5.5, True), (True, False), ("5", False), (None, False)],
)
def test_is_number(value, expected):
    assert _is_number(value) is expected


# --- chain rules ---

CHAINS = """
rules:
  - {name: allow-email, tool: send_email, decision: allow}
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


def test_fired_chain_decides_then_tool(load):
    rules = load(CHAINS)
    fired = {"no-send-after-secrets"}
    assert rules.decide("send_email", {}, fired) == Decision(
        "block", "no-send-after-secrets"
    )
    assert rules.decide("send_sms", {}, fired).decision == "block"


def test_unfired_chain_has_no_effect(load):
    rules = load(CHAINS)
    assert rules.decide("send_email", {}) == Decision("allow", "allow-email")
    assert rules.decide("send_sms", {}) == Decision("allow", None)


def test_chain_is_checked_before_normal_rules(load):
    # allow-email would allow send_email, but the fired chain wins.
    rules = load(CHAINS)
    decision = rules.decide("send_email", {}, {"no-send-after-secrets"})
    assert decision.rule_name == "no-send-after-secrets"


def test_fired_chain_ignores_other_tools(load):
    rules = load(CHAINS)
    fired = {"no-send-after-secrets"}
    assert rules.decide("read_file", {"path": "a"}, fired) == Decision("allow", None)


def test_chains_fired_by_uses_when(load):
    rules = load(CHAINS)
    assert rules.chains_fired_by("read_file", {"path": "/app/.env"}) == {
        "no-send-after-secrets"
    }
    assert rules.chains_fired_by("read_file", {"path": "notes.txt"}) == set()
    assert rules.chains_fired_by("read_file", {}) == set()
    assert rules.chains_fired_by("get_user", {"path": ".env"}) == set()


def test_chain_after_without_when(load):
    rules = load(
        "chains:\n  - {name: c, after: {tool: 'read_*'},"
        " then: {tool: send_email, decision: ask}}\n"
    )
    assert rules.chains_fired_by("read_db", {}) == {"c"}
    assert rules.decide("send_email", {}, {"c"}) == Decision("ask", "c")


@pytest.mark.parametrize(
    ("chains", "message"),
    [
        ("chains: {a: 1}", "'chains' must be a list"),
        (
            (
                "chains:\n  - {name: c, after: {tool: x}, then: {tool: y, decision: block},"
                " extra: 1}"
            ),
            "chain 'c' \\(#1\\): unknown key",
        ),
        (
            "chains:\n  - {name: c, then: {tool: y, decision: block}}",
            "chain 'c' \\(#1\\): missing key",
        ),
        (
            (
                "chains:\n  - {name: c, after: {tool: x},"
                " then: {tool: y, decision: block, when: {}}}"
            ),
            "chain 'c' \\(#1\\) then: unknown key",
        ),
        (
            "chains:\n  - {name: c, after: {tool: x}, then: {tool: y, decision: nope}}",
            "chain 'c' \\(#1\\) then: decision must be one of",
        ),
        (
            (
                "chains:\n  - {name: c, after: {tool: x, when: {p: {equals: 1}}},"
                " then: {tool: y, decision: block}}"
            ),
            "chain 'c' \\(#1\\): unknown condition 'equals'",
        ),
        (
            "chains:\n  - {name: c, after: x, then: {tool: y, decision: block}}",
            "chain 'c' \\(#1\\) after: must be a mapping",
        ),
    ],
)
def test_bad_chains(load, chains, message):
    with pytest.raises(RulesError, match=message):
        load(chains)


def test_example_rules_file_chain():
    rules = load_rules(EXAMPLE)
    fired = rules.chains_fired_by("read_file", {"path": ".env"})
    assert fired == {"no-send-after-secrets"}
    assert rules.decide("send_email", {}, fired) == Decision(
        "block", "no-send-after-secrets"
    )


def test_call_matches():
    when = {"path": ("matches", "*.env")}
    assert _call_matches("read_*", when, "read_file", {"path": "a.env"})
    assert not _call_matches("read_*", when, "read_file", {"path": "a.txt"})
    assert not _call_matches("read_*", when, "write_file", {"path": "a.env"})
    assert not _call_matches("read_*", when, "read_file", {})


def test_label():
    assert _label("chain", 2, {"name": "c"}) == "chain 'c' (#2)"
    assert _label("rule", 1, {}) == "rule #1"
    assert _label("rule", 1, "not a dict") == "rule #1"
