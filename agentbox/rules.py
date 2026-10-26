"""Allow / block / ask rules for tool calls, loaded from a YAML file."""

import math
import operator
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path

import yaml

DECISIONS = {"allow", "block", "ask"}
RULE_KEYS = {"name", "tool", "when", "decision"}


def _contains(actual, expected) -> bool:
    return expected in actual


def _matches(actual, pattern) -> bool:
    # Glob match, case-insensitive. Only strings can match.
    return isinstance(actual, str) and fnmatchcase(actual.lower(), str(pattern).lower())


# Condition name -> check(actual_arg_value, value_from_yaml).
CONDITIONS = {
    "eq": operator.eq,
    "ne": operator.ne,
    "gt": operator.gt,
    "gte": operator.ge,
    "lt": operator.lt,
    "lte": operator.le,
    "contains": _contains,
    "matches": _matches,
}

NUMBER_CONDITIONS = {"gt", "gte", "lt", "lte"}


class RulesError(Exception):
    """The rules file is invalid."""


@dataclass(frozen=True)
class Decision:
    decision: str
    rule_name: str | None


@dataclass
class Rule:
    name: str
    tool: str
    decision: str
    # Argument name -> (condition name, expected value).
    when: dict[str, tuple[str, object]] = field(default_factory=dict)

    def matches(self, tool_name: str, args: dict) -> bool:
        """True if the tool name matches and every condition holds."""
        if not fnmatchcase(tool_name, self.tool):
            return False
        for arg_name, (op, expected) in self.when.items():
            if arg_name not in args:
                return False
            if not _check(op, args[arg_name], expected):
                return False
        return True


@dataclass
class Rules:
    default: str = "allow"
    rules: list[Rule] = field(default_factory=list)

    def decide(self, tool_name: str, args: dict) -> Decision:
        """Return the decision of the first matching rule, else the default."""
        for rule in self.rules:
            if rule.matches(tool_name, args):
                return Decision(rule.decision, rule.name)
        return Decision(self.default, None)


def load_rules(path: str | Path) -> Rules:
    """Read and validate a rules YAML file. Raises RulesError if it's invalid."""
    text = Path(path).read_text()
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise RulesError(f"invalid YAML in {path}: {e}") from e
    return _parse(data)


def _check(op: str, actual, expected) -> bool:
    """Run one condition. Values that can't be compared (e.g. "a" > 5) don't match."""
    # LLMs often send numbers as strings ("8000"). Convert them for number checks,
    # otherwise "8000" > 5000 can't be compared and a big amount slips through.
    if op in NUMBER_CONDITIONS and isinstance(actual, str) and _is_number(expected):
        actual = _to_number(actual)
        if actual is None:
            return False
    try:
        return bool(CONDITIONS[op](actual, expected))
    except TypeError:
        return False


def _is_number(value) -> bool:
    # bool is a subclass of int in Python, but True/False aren't amounts.
    return isinstance(value, int | float) and not isinstance(value, bool)


def _to_number(text: str) -> float | None:
    """Turn "8000" or " 12.5 " into a float. None if it's not a number or is NaN."""
    try:
        number = float(text)
    except ValueError:
        return None
    # float("nan") parses fine, but NaN compares False to everything.
    if math.isnan(number):
        return None
    return number


def _parse(data) -> Rules:
    if data is None:  # empty file
        return Rules()
    if not isinstance(data, dict):
        raise RulesError("rules file must be a mapping with 'default' and 'rules'")

    default = data.get("default", "allow")
    if default not in DECISIONS:
        raise RulesError(f"default must be one of {sorted(DECISIONS)}, got {default!r}")

    raw_rules = data.get("rules", [])
    if not isinstance(raw_rules, list):
        raise RulesError("'rules' must be a list")

    rules = [_parse_rule(i, raw) for i, raw in enumerate(raw_rules, start=1)]
    return Rules(default=default, rules=rules)


def _parse_rule(index: int, raw) -> Rule:
    if not isinstance(raw, dict):
        raise RulesError(f"rule #{index}: must be a mapping")

    label = f"rule {raw['name']!r} (#{index})" if "name" in raw else f"rule #{index}"

    unknown = set(raw) - RULE_KEYS
    if unknown:
        raise RulesError(f"{label}: unknown key(s) {sorted(unknown)}")
    for key in ("name", "tool"):
        if not isinstance(raw.get(key), str):
            raise RulesError(f"{label}: '{key}' must be a string")
    if raw.get("decision") not in DECISIONS:
        raise RulesError(
            f"{label}: decision must be one of {sorted(DECISIONS)},"
            f" got {raw.get('decision')!r}"
        )

    raw_when = raw.get("when") or {}
    if not isinstance(raw_when, dict):
        raise RulesError(f"{label}: 'when' must be a mapping")

    when = {}
    for arg_name, condition in raw_when.items():
        # Each argument gets exactly one condition, e.g. amount: { gt: 5000 }.
        if not isinstance(condition, dict) or len(condition) != 1:
            raise RulesError(
                f"{label}: condition for {arg_name!r} must have exactly one key,"
                " e.g. { gt: 5000 }"
            )
        [(op, expected)] = condition.items()
        if op not in CONDITIONS:
            raise RulesError(
                f"{label}: unknown condition {op!r}; use one of {list(CONDITIONS)}"
            )
        when[arg_name] = (op, expected)

    return Rule(name=raw["name"], tool=raw["tool"], decision=raw["decision"], when=when)
