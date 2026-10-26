"""Allow / block / ask rules for tool calls, loaded from a YAML file."""

import math
import operator
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path

import yaml

DECISIONS = {"allow", "block", "ask"}
RULE_KEYS = {"name", "tool", "when", "decision"}
CHAIN_KEYS = {"name", "after", "then"}


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
        return _call_matches(self.tool, self.when, tool_name, args)


@dataclass
class Chain:
    """After `after_tool` succeeds in a run, apply `decision` to `then_tool`."""

    name: str
    after_tool: str
    then_tool: str
    decision: str
    after_when: dict[str, tuple[str, object]] = field(default_factory=dict)

    def fired_by(self, tool_name: str, args: dict) -> bool:
        """True if this call (once it succeeds) fires the chain."""
        return _call_matches(self.after_tool, self.after_when, tool_name, args)

    def applies_to(self, tool_name: str) -> bool:
        """True if a fired chain should decide for this tool."""
        return fnmatchcase(tool_name, self.then_tool)


@dataclass
class Rules:
    default: str = "allow"
    rules: list[Rule] = field(default_factory=list)
    chains: list[Chain] = field(default_factory=list)

    def decide(
        self, tool_name: str, args: dict, fired: set[str] | frozenset = frozenset()
    ) -> Decision:
        """Decide for one call. `fired` holds the names of chains fired in this run.

        Fired chains are checked first, then normal rules (first match wins),
        then the default.
        """
        for chain in self.chains:
            if chain.name in fired and chain.applies_to(tool_name):
                return Decision(chain.decision, chain.name)
        for rule in self.rules:
            if rule.matches(tool_name, args):
                return Decision(rule.decision, rule.name)
        return Decision(self.default, None)

    def chains_fired_by(self, tool_name: str, args: dict) -> set[str]:
        """Names of the chains that a successful call like this one fires."""
        return {c.name for c in self.chains if c.fired_by(tool_name, args)}


def load_rules(path: str | Path) -> Rules:
    """Read and validate a rules YAML file. Raises RulesError if it's invalid."""
    text = Path(path).read_text()
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise RulesError(f"invalid YAML in {path}: {e}") from e
    return _parse(data)


def _call_matches(tool_glob: str, when: dict, tool_name: str, args: dict) -> bool:
    """Shared by rules and chains: tool glob matches and every condition holds."""
    if not fnmatchcase(tool_name, tool_glob):
        return False
    for arg_name, (op, expected) in when.items():
        if arg_name not in args:
            return False
        if not _check(op, args[arg_name], expected):
            return False
    return True


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

    raw_chains = data.get("chains", [])
    if not isinstance(raw_chains, list):
        raise RulesError("'chains' must be a list")

    rules = [_parse_rule(i, raw) for i, raw in enumerate(raw_rules, start=1)]
    chains = [_parse_chain(i, raw) for i, raw in enumerate(raw_chains, start=1)]
    return Rules(default=default, rules=rules, chains=chains)


def _parse_rule(index: int, raw) -> Rule:
    label = _label("rule", index, raw)
    _check_keys(label, raw, allowed=RULE_KEYS)
    _check_strings(label, raw, ("name", "tool"))
    _check_decision(label, raw.get("decision"))
    return Rule(
        name=raw["name"],
        tool=raw["tool"],
        decision=raw["decision"],
        when=_parse_when(label, raw.get("when")),
    )


def _parse_chain(index: int, raw) -> Chain:
    label = _label("chain", index, raw)
    _check_keys(label, raw, allowed=CHAIN_KEYS, required=CHAIN_KEYS)
    _check_strings(label, raw, ("name",))

    after, then = raw["after"], raw["then"]
    _check_keys(f"{label} after", after, allowed={"tool", "when"}, required={"tool"})
    _check_strings(f"{label} after", after, ("tool",))
    _check_keys(f"{label} then", then, allowed={"tool", "decision"}, required={"tool"})
    _check_strings(f"{label} then", then, ("tool",))
    _check_decision(f"{label} then", then.get("decision"))

    return Chain(
        name=raw["name"],
        after_tool=after["tool"],
        after_when=_parse_when(label, after.get("when")),
        then_tool=then["tool"],
        decision=then["decision"],
    )


def _label(kind: str, index: int, raw) -> str:
    """e.g. "rule 'no-deletes' (#1)", used at the start of error messages."""
    if isinstance(raw, dict) and "name" in raw:
        return f"{kind} {raw['name']!r} (#{index})"
    return f"{kind} #{index}"


def _check_keys(label: str, raw, allowed: set, required: set = frozenset()) -> None:
    if not isinstance(raw, dict):
        raise RulesError(f"{label}: must be a mapping")
    unknown = set(raw) - allowed
    if unknown:
        raise RulesError(f"{label}: unknown key(s) {sorted(unknown)}")
    missing = required - set(raw)
    if missing:
        raise RulesError(f"{label}: missing key(s) {sorted(missing)}")


def _check_strings(label: str, raw: dict, keys) -> None:
    for key in keys:
        if not isinstance(raw.get(key), str):
            raise RulesError(f"{label}: '{key}' must be a string")


def _check_decision(label: str, decision) -> None:
    if decision not in DECISIONS:
        raise RulesError(
            f"{label}: decision must be one of {sorted(DECISIONS)}, got {decision!r}"
        )


def _parse_when(label: str, raw_when) -> dict[str, tuple[str, object]]:
    """Parse a `when` mapping. Shared by rules and chains."""
    raw_when = raw_when or {}
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
    return when
