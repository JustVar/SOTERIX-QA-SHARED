"""Pure helpers for reading a node id -> case key mapping.

Only the parametrization-collapsing helpers live here. The *policy* that
decides which mapping rows may upload does NOT: the UI repo gates on a
`reviewed` flag, the API repo does not, and the archive's display-only
reader is permissive by contract. That policy is injected by each repo's
shim (see the package README, rule 8), so this module stays free of any
opinion about which rows count.

Nothing here touches the filesystem or the network.
"""
from __future__ import annotations

import re

# Greedy and anchored, so a node id whose parameter itself contains a
# bracket collapses to the bare function rather than to a fragment.
_PARAM_RE = re.compile(r'\[.*\]$')


def function_base(nodeid: str) -> str:
    """Strip the [param] suffix: file::Class::test_x[chromium] -> file::Class::test_x."""
    return _PARAM_RE.sub('', nodeid)


def base_keyed(mapping: dict[str, str]) -> dict[str, str]:
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
    collapsed: dict[str, str] = {}
    conflicting: set[str] = set()
    for nodeid, case_key in mapping.items():
        base = function_base(nodeid)
        if base in collapsed and collapsed[base] != case_key:
            conflicting.add(base)
        collapsed[base] = case_key
    for base in conflicting:
        collapsed.pop(base, None)
    return collapsed
