"""Mapping QA Touch data onto a Jira project's custom fields.

Every field id here is discovered, not hard-coded, so the whole module has
to behave sensibly when the discovered file is absent, stale or malformed.
That is the normal state on a fresh checkout.
"""
from __future__ import annotations

import json

import pytest

from soterix_qa_shared.jira import fields as jf
from soterix_qa_shared.jira.fields import QATouchToJiraMapper

DISCOVERED = {
    'issue_types': {
        'Bug': {
            'fields': {
                'customfield_101': {'name': 'Module', 'required': True,
                                    'allowed_values': [{'id': '1', 'value': 'Views'},
                                                       {'id': '2', 'value': 'Events'}]},
                'customfield_102': {'name': 'Steps to Reproduce', 'required': True},
                'customfield_103': {'name': 'Bug Overview', 'required': True},
                'customfield_104': {'name': 'Expected Behavior', 'required': False},
                'customfield_105': {'name': 'Actual Behavior', 'required': False},
                'customfield_106': {'name': 'Environment Found', 'required': True,
                                    'allowed_values': [{'id': '9', 'value': 'QA'}]},
                'customfield_107': {'name': 'Severity', 'required': True,
                                    'allowed_values': [{'value': 'High'},
                                                       {'value': 'Medium'}]},
                'customfield_108': {'name': 'Affected Build Number', 'required': False},
                'customfield_109': {'name': 'Root Cause', 'required': True},
                'summary': {'name': 'Summary', 'required': True},
            }
        }
    }
}


@pytest.fixture(autouse=True)
def _clean():
    yield
    jf.MAPPING_JSON = None
    jf.reset_cache()


@pytest.fixture
def discovered(tmp_path):
    path = tmp_path / 'jira_field_mapping.json'
    path.write_text(json.dumps(DISCOVERED), encoding='utf-8')
    jf.configure(path)
    return path


def _mapper():
    return QATouchToJiraMapper()


# ---------------------------------------------------------------------------
# Loading, and the states a fresh checkout is actually in
# ---------------------------------------------------------------------------

def test_without_configure_nothing_is_discovered():
    """The package holds no path. A guess would resolve inside
    site-packages and find nothing, silently."""
    jf.MAPPING_JSON = None
    jf.reset_cache()
    assert jf.load_discovered_fields() == {}
    assert jf.get_required_field_ids('Bug') == {}


def test_a_missing_file_is_not_an_error(tmp_path):
    jf.configure(tmp_path / 'never-ran-discovery.json')
    assert jf.load_discovered_fields() == {}


def test_a_malformed_file_is_not_an_error(tmp_path):
    """A half-written discovery run must not take a bug report down."""
    path = tmp_path / 'jira_field_mapping.json'
    path.write_text('{ this is not json', encoding='utf-8')
    jf.configure(path)
    assert jf.load_discovered_fields() == {}


def test_a_file_holding_a_list_is_not_an_error(tmp_path):
    """Valid JSON of the wrong shape. .get() on a list raises."""
    path = tmp_path / 'jira_field_mapping.json'
    path.write_text('[1, 2, 3]', encoding='utf-8')
    jf.configure(path)
    assert jf.load_discovered_fields() == {}
    assert jf.get_required_field_ids('Bug') == {}


def test_the_file_is_read_once_not_once_per_lookup(discovered, monkeypatch):
    """map_bug_fields used to re-read this JSON more than a dozen times,
    and the mapper held a constructor snapshot that could disagree with
    what those reads returned."""
    jf.reset_cache()
    reads = []
    real_open = open

    def counting_open(*args, **kwargs):
        reads.append(args[0])
        return real_open(*args, **kwargs)

    monkeypatch.setattr('builtins.open', counting_open)
    m = _mapper()
    m.map_bug_fields(module='Views', steps='1. Login', expected='e', actual='a',
                     environment='QA', severity='High', build='v1')
    assert len(reads) == 1


def test_reset_cache_picks_up_a_rewritten_file(discovered):
    """A discovery run rewrites the file mid-process."""
    assert 'Module' in jf.get_required_field_ids('Bug')
    discovered.write_text(json.dumps({'issue_types': {}}), encoding='utf-8')
    assert 'Module' in jf.get_required_field_ids('Bug')     # still cached
    jf.reset_cache()
    assert jf.get_required_field_ids('Bug') == {}


def test_setting_the_path_directly_still_reloads(tmp_path, discovered):
    """configure() clears the cache, so it cannot show this. Assigning
    MAPPING_JSON straight is a real pattern - test fixtures do it, and a
    consumer reading the module can too - and without the path check it
    would serve the previous project's field ids for the rest of the
    process."""
    assert 'Module' in jf.get_required_field_ids('Bug')     # warms the cache
    other = tmp_path / 'other.json'
    other.write_text(json.dumps({'issue_types': {}}), encoding='utf-8')
    jf.MAPPING_JSON = other                                 # no reset_cache()
    assert jf.get_required_field_ids('Bug') == {}


def test_configure_to_a_new_path_reloads(tmp_path, discovered):
    other = tmp_path / 'other.json'
    other.write_text(json.dumps({'issue_types': {}}), encoding='utf-8')
    jf.configure(other)
    assert jf.get_required_field_ids('Bug') == {}


# ---------------------------------------------------------------------------
# Field lookup
# ---------------------------------------------------------------------------

def test_required_field_ids_are_names_to_ids(discovered):
    required = jf.get_required_field_ids('Bug')
    assert required['Module'] == 'customfield_101'
    assert 'Expected Behavior' not in required        # not required
    assert required['Root Cause'] == 'customfield_109'


def test_an_unknown_issue_type_has_no_fields(discovered):
    assert jf.get_required_field_ids('Epic') == {}
    assert jf.get_field_allowed_values('Epic', 'customfield_101') == []


def test_allowed_values_come_from_the_discovered_data(discovered):
    values = jf.get_field_allowed_values('Bug', 'customfield_101')
    assert [v['value'] for v in values] == ['Views', 'Events']


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------

def test_map_bug_fields_fills_what_it_can(discovered):
    mapped = _mapper().map_bug_fields(
        module='Views', steps='1. Login', expected='loads', actual='black',
        environment='QA', severity='High', build='v1.50.0', title='Feed broken')
    assert mapped['customfield_101'] == {'id': '1'}          # select, by id
    assert mapped['customfield_102'] == '1. Login'
    assert mapped['customfield_103'] == 'Feed broken'
    assert mapped['customfield_104'] == 'loads'
    assert mapped['customfield_105'] == 'black'
    assert mapped['customfield_106'] == {'id': '9'}
    assert mapped['customfield_107'] == {'value': 'High'}    # no id, so by value
    assert mapped['customfield_108'] == 'v1.50.0'


def test_a_test_case_id_is_prefixed_onto_the_overview(discovered):
    mapped = _mapper().map_bug_fields(title='Feed broken', test_case_id='TR0262')
    assert mapped['customfield_103'] == '[TR0262] Feed broken'


def test_an_empty_title_still_gets_an_overview(discovered):
    """Bug Overview is required. An empty one is a rejected create."""
    assert _mapper().map_bug_fields()['customfield_103'] == 'Bug found during QA testing'


def test_severity_maps_through_the_priority_table(discovered):
    """QA Touch says Critical; this Jira calls it Highest. Neither is in
    the other's vocabulary."""
    assert jf.PRIORITY_TO_SEVERITY['Critical'] == 'Highest'
    mapped = _mapper().map_bug_fields(severity='Critical')
    assert mapped['customfield_107'] in ({'value': 'High'}, {'value': 'Medium'})


def test_an_unknown_select_value_falls_back_to_the_first_allowed(discovered):
    """A wrong-but-valid value is visible in the ticket. A rejected create
    helps nobody."""
    assert _mapper().map_bug_fields(module='Nonexistent')['customfield_101'] == {'id': '1'}


def test_a_partial_name_matches_an_allowed_value(discovered):
    assert _mapper().map_bug_fields(module='view')['customfield_101'] == {'id': '1'}


def test_empty_values_are_left_out_entirely(discovered):
    """A field sent as an empty string still counts as sent, and reads as
    filled in the ticket."""
    mapped = _mapper().map_bug_fields(module='Views', steps='', expected='')
    assert 'customfield_102' not in mapped
    assert 'customfield_104' not in mapped


def test_with_nothing_discovered_nothing_maps():
    jf.MAPPING_JSON = None
    jf.reset_cache()
    assert _mapper().map_bug_fields(module='Views', steps='1. Login') == {}


# ---------------------------------------------------------------------------
# What the tester still has to fill in
# ---------------------------------------------------------------------------

def test_unmapped_required_fields_name_what_is_missing(discovered):
    m = _mapper()
    mapped = m.map_bug_fields(module='Views', steps='s', environment='QA',
                              severity='High', title='t')
    unmapped = m.get_unmapped_required_fields('Bug', mapped)
    assert unmapped == ['Root Cause (customfield_109)']


def test_standard_fields_are_never_reported_as_unmapped(discovered):
    """summary is required and the caller always sets it. Reporting it
    would train people to ignore the list."""
    m = _mapper()
    unmapped = m.get_unmapped_required_fields('Bug', {})
    assert not any(u.startswith('Summary') for u in unmapped)
    assert 'Module (customfield_101)' in unmapped


def test_the_mappers_own_cache_can_be_dropped(discovered):
    m = _mapper()
    assert 'Module' in m._get_required_fields('Bug')
    jf.configure(discovered.parent / 'gone.json')
    m.reset()
    assert m._get_required_fields('Bug') == {}
