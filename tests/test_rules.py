from pathlib import Path

import pytest

from agentbox.rules import Decision, RulesError, _is_number, _to_number, load_rules

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
