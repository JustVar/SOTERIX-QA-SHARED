"""Mapping QA Touch test data onto a Jira project's custom fields.

A Jira project's mandatory Bug fields are custom fields with generated ids
(`customfield_10234`), and the ids differ per project. A discovery pass
writes them to a JSON file; this module reads that file and maps a test's
data onto whatever it found. Nothing here is hard-coded to one project: with
no discovered file, every lookup returns nothing and the caller learns which
required fields it must fill by hand.

The path to that file is the consumer's, supplied through `configure()`.
`__file__` points into site-packages after installation, so the package
cannot find a repo's config directory on its own.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Set by the consumer's shim or CLI. No default: a package-level guess would
# resolve inside site-packages and silently find nothing.
MAPPING_JSON: Path | None = None

_CACHE: dict[str, Any] | None = None
_CACHE_KEY: str | None = None


def configure(path: Path | str) -> None:
    """Point the field loader at a consumer's discovered-fields JSON."""
    global MAPPING_JSON
    MAPPING_JSON = Path(path)
    reset_cache()


def reset_cache() -> None:
    """Forget the loaded file. Call after a discovery run rewrites it."""
    global _CACHE, _CACHE_KEY
    _CACHE = None
    _CACHE_KEY = None


QA_TOUCH_TO_JIRA_STATUS = {
    'Passed': 'Complete',
    'Failed': 'QA Not Passed',
    'Blocked': 'Blocked',
    'Skipped': None,        # no Jira action
    'Not Tested': None,
}

JIRA_TO_QA_TOUCH_STATUS = {
    'Complete': 'Passed',
    'QA Not Passed': 'Failed',
    'Blocked': 'Blocked',
}

NC_ISSUE_TYPES = {
    'Story': {'qa_creates': False, 'qa_tests': True},
    'Bug': {'qa_creates': True, 'qa_tests': True},
    'Child Bug': {'qa_creates': True, 'qa_tests': True},
    'Task': {'qa_creates': False, 'qa_tests': True},
    'Hotfix': {'qa_creates': False, 'qa_tests': False, 'note': 'Support verifies'},
    'Spike': {'qa_creates': False, 'qa_tests': False},
    'Epic': {'qa_creates': False, 'qa_tests': False},
    'Sub-task': {'qa_creates': False, 'qa_tests': True},
}

PRIORITY_TO_SEVERITY = {
    'Critical': 'Highest',
    'High': 'High',
    'Medium': 'Medium',
    'Low': 'Low',
    'Trivial': 'Lowest',
}

# Standard fields the caller always sets, so they are never "unmapped".
ALWAYS_SET = ('project', 'issuetype', 'summary', 'description')


def load_discovered_fields() -> dict[str, Any]:
    """The discovered field data, or {} when there is none.

    Cached, keyed by the configured path. Before this, every lookup re-read
    the file from disk: one `map_bug_fields` call did more than a dozen
    reads of the same JSON, and the mapper held a snapshot from its own
    constructor that could disagree with what those reads returned.
    """
    global _CACHE, _CACHE_KEY
    key = str(MAPPING_JSON) if MAPPING_JSON else ''
    if _CACHE is not None and _CACHE_KEY == key:
        return _CACHE
    data: dict[str, Any] = {}
    if MAPPING_JSON and MAPPING_JSON.exists():
        try:
            with open(MAPPING_JSON, encoding='utf-8') as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, json.JSONDecodeError, ValueError):
            # A malformed file means no field ids, which the caller already
            # handles: it reports every required field as unmapped. Raising
            # here would take a bug report down with it.
            data = {}
    _CACHE, _CACHE_KEY = data, key
    return data


def _fields_for(issue_type: str) -> dict[str, Any]:
    return (load_discovered_fields()
            .get('issue_types', {})
            .get(issue_type, {})
            .get('fields', {}))


def get_required_field_ids(issue_type: str = 'Bug') -> dict[str, str]:
    """{field name: field id} for the required fields of an issue type."""
    return {info['name']: fid
            for fid, info in _fields_for(issue_type).items()
            if info.get('required') and info.get('name')}


def get_field_allowed_values(issue_type: str, field_id: str) -> list[dict]:
    """The allowed values of a select field, from the discovered data."""
    return _fields_for(issue_type).get(field_id, {}).get('allowed_values', [])


class QATouchToJiraMapper:
    """Maps QA Touch test case data onto Jira issue fields.

    Every lookup goes through the module's cached view, so the mapper cannot
    hold a snapshot that disagrees with what a lookup returns.
    """

    def __init__(self) -> None:
        self._required_fields_cache: dict[str, dict[str, str]] = {}

    def _get_required_fields(self, issue_type: str) -> dict[str, str]:
        if issue_type not in self._required_fields_cache:
            self._required_fields_cache[issue_type] = get_required_field_ids(issue_type)
        return self._required_fields_cache[issue_type]

    def reset(self) -> None:
        """Drop the per-instance cache. Pairs with fields.reset_cache()."""
        self._required_fields_cache.clear()

    def _find_field_id(self, issue_type: str, name_pattern: str) -> str | None:
        """A field id whose name contains `name_pattern`, required first."""
        pattern = name_pattern.lower()
        for name, fid in self._get_required_fields(issue_type).items():
            if pattern in name.lower():
                return fid
        for fid, info in _fields_for(issue_type).items():
            if pattern in str(info.get('name', '')).lower():
                return fid
        return None

    def _match_allowed_value(self, issue_type: str, field_id: str,
                             search_value: str) -> dict | None:
        """The best allowed value for a select field.

        Exact match, then partial, then the first value. The fallback is
        deliberate: a Bug that Jira rejects for a bad select value helps
        nobody, and a wrong-but-valid value is visible in the ticket.
        """
        allowed = get_field_allowed_values(issue_type, field_id)
        if not allowed:
            return None
        search = search_value.lower().strip()
        for av in allowed:
            if str(av.get('value') or av.get('name') or '').lower() == search:
                return av
        for av in allowed:
            val = str(av.get('value') or av.get('name') or '').lower()
            if val and (search in val or val in search):
                return av
        return allowed[0]

    def _put_choice(self, mapped: dict[str, Any], issue_type: str,
                    fid: str, value: str) -> None:
        """Write a select field as Jira wants it: by id, else by value."""
        av = self._match_allowed_value(issue_type, fid, value)
        if not av:
            return
        if 'id' in av:
            mapped[fid] = {'id': av['id']}
        elif 'value' in av:
            mapped[fid] = {'value': av['value']}

    def map_bug_fields(
        self,
        module: str = '',
        steps: str = '',
        expected: str = '',
        actual: str = '',
        environment: str = 'QA',
        severity: str = 'Medium',
        build: str = '',
        title: str = '',
        test_case_id: str = '',
    ) -> dict[str, Any]:
        """Map QA Touch test data to {field_id: value} for create_issue.

        Only fields with both a known id and a value are included. What is
        left out is reported by `get_unmapped_required_fields`, so a tester
        knows what to fill rather than meeting a Jira validation error.
        """
        mapped: dict[str, Any] = {}
        it = 'Bug'

        fid = self._find_field_id(it, 'module')
        if fid and module:
            self._put_choice(mapped, it, fid, module)

        fid = self._find_field_id(it, 'steps to reproduce')
        if fid and steps:
            mapped[fid] = steps

        fid = self._find_field_id(it, 'bug overview')
        if fid:
            overview = title or 'Bug found during QA testing'
            if test_case_id:
                overview = f'[{test_case_id}] {overview}'
            mapped[fid] = overview

        fid = self._find_field_id(it, 'expected')
        if fid and expected:
            mapped[fid] = expected

        fid = self._find_field_id(it, 'actual')
        if fid and actual:
            mapped[fid] = actual

        fid = self._find_field_id(it, 'environment')
        if fid and environment:
            self._put_choice(mapped, it, fid, environment)

        fid = self._find_field_id(it, 'severity')
        if fid and severity:
            self._put_choice(mapped, it, fid,
                             PRIORITY_TO_SEVERITY.get(severity, severity))

        fid = (self._find_field_id(it, 'affected build')
               or self._find_field_id(it, 'build number'))
        if fid and build:
            mapped[fid] = build

        return mapped

    def get_unmapped_required_fields(self, issue_type: str,
                                     mapped_fields: dict) -> list[str]:
        """Required fields that were NOT populated, as "Name (id)".

        This is what a tester reads to know the ticket needs more work.
        """
        return [f'{name} ({fid})'
                for name, fid in self._get_required_fields(issue_type).items()
                if fid not in ALWAYS_SET and fid not in mapped_fields]


# One mapper is enough, and building it costs nothing: it reads no file
# until a lookup asks for one.
mapper = QATouchToJiraMapper()
