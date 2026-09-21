"""Pure helpers for reading a node id -> case key mapping.

Only the parametrization-collapsing helpers live here. The *policy* that
decides which mapping rows may upload does NOT: both consumer repos gate
on a `reviewed` flag, and the archive's display-only reader is permissive
by contract. That policy is injected by each repo's shim (see
docs/RULES.md, rule 8), so this module stays free of any opinion about
which rows count.

Nothing here touches the filesystem or the network.
"""
from __future__ import annotations

import re
from collections.abc import Mapping

# A row's value: one case key, or one per QA Touch project for a test that
# runs against more than one product (the Core UI tests run against the
# Neurosen private cloud too, which is its own project). The reporter
# picks the key for the project the run reports to; a str is that key
# whatever the project.
MappingValue = 'str | Mapping[str, str]'

# Greedy and anchored, so a node id whose parameter itself contains a
# bracket collapses to the bare function rather than to a fragment.
_PARAM_RE = re.compile(r'\[.*\]$')


def canonical_nodeid(nodeid: str) -> str:
    """The node id without the '@<group>' that pytest-xdist adds under --dist loadgroup.

    A test with an xdist_group mark reports as 'file::test_x[chromium]@account_views'.
    The same test had no suffix before it got the mark, and has none in a run with no -n.
    Read as 2 tests, its history split in 2, the health page listed it 2 times, and
    the '[param]$' rule below stopped matching, so its QA Touch case was not found
    (found 09/21/2026). A parameter can hold an '@' ('test[a@b.com]'): only a last
    part with no bracket in it is a group.
    """
    head, sep, tail = nodeid.rpartition('@')
    if sep and head and '[' not in tail and ']' not in tail and '::' not in tail:
        return head
    return nodeid


def function_base(nodeid: str) -> str:
    """Strip the group and the [param] suffix: file::Class::test_x[chromium]@g -> file::Class::test_x."""
    return _PARAM_RE.sub('', canonical_nodeid(nodeid))


def key_for(value: str | Mapping[str, str] | None, project: str | None) -> str | None:
    """The case key a mapping value gives for a project: a str is the key
    for every project; a per-project map answers for its projects only."""
    if value is None:
        return None
    if isinstance(value, Mapping):
        key = value.get(str(project or ''))
        return str(key) if key else None
    return str(value) or None


def base_keyed(mapping: dict[str, str | Mapping[str, str]]) -> dict[str, str | Mapping[str, str]]:
    """function_base(nodeid) -> case_key, for rows that agree.

    Mapping keys carry the parametrization they were generated under: in the
    UI repo 377 of 378 rows end in [chromium], because that is what
    pytest.ini pins. A reporter that strips the parameter from the RUNTIME
    node id still misses those stored keys, so running the same suite under
    another browser matched 0 of 121 reviewed rows, and an Appium suite
    (no bracket at all) would match none either.

    Collapsing both sides to the bare function makes the mapping
    parametrization-agnostic. A base whose rows disagree on the case key is
    left OUT: that is a genuine ambiguity the exact match must settle, and
    guessing would write a status onto the wrong case - worse than writing
    none at all.
    """
    collapsed: dict[str, str | Mapping[str, str]] = {}
    conflicting: set[str] = set()
    for nodeid, case_key in mapping.items():
        base = function_base(nodeid)
        if base in collapsed and collapsed[base] != case_key:
            conflicting.add(base)
        collapsed[base] = case_key
    for base in conflicting:
        collapsed.pop(base, None)
    return collapsed
