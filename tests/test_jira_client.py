"""The Jira client.

Filing a bug is the one action in this pipeline another team sees, so the
tests here are mostly about not doing it: not filing when disabled, not
losing an issue key when a create succeeds, not leaving a test bug
unmarked in somebody's backlog.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from soterix_qa_shared.jira import client as jc
from soterix_qa_shared.jira.client import TEST_LABEL, JiraClient


@pytest.fixture(autouse=True)
def _disabled(monkeypatch):
    """Nothing reaches Jira unless a test says so."""
    monkeypatch.delenv('BUG_CREATION_ENABLED', raising=False)
    monkeypatch.setattr(jc, 'JIRA', None)
    yield


def _live(monkeypatch, issue_key='NC-123'):
    """Arm a real create against a fake jira package."""
    monkeypatch.setenv('BUG_CREATION_ENABLED', 'true')
    issue = MagicMock()
    issue.key = issue_key
    issue.fields = SimpleNamespace(labels=[])
    api = MagicMock()
    api.create_issue.return_value = issue
    monkeypatch.setattr(jc, 'JIRA', MagicMock(return_value=api))
    return api, issue


# ---------------------------------------------------------------------------
# The double gate
# ---------------------------------------------------------------------------

def test_disabled_by_default_returns_a_dry_run(caplog):
    with caplog.at_level(logging.INFO, logger='soterix_qa_shared.jira'):
        result = JiraClient().create_issue('a summary', 'a description')
    assert result['dry_run'] is True
    assert result['reason'] == 'bug creation is disabled'
    assert 'Would create' in caplog.text


def test_enabled_without_the_jira_package_still_files_nothing(monkeypatch):
    """The env says yes and the dependency says no. Saying which one is
    missing is the difference between a one-line fix and an afternoon."""
    monkeypatch.setenv('BUG_CREATION_ENABLED', 'true')
    monkeypatch.setattr(jc, 'JIRA', None)
    result = JiraClient().create_issue('a summary', 'a description')
    assert result['dry_run'] is True
    assert result['reason'] == 'the jira package is not installed'


@pytest.mark.parametrize('value', ['1', 'true', 'TRUE', 'yes'])
def test_the_enable_flag_accepts_the_documented_spellings(monkeypatch, value):
    monkeypatch.setenv('BUG_CREATION_ENABLED', value)
    assert jc.creation_enabled() is True


@pytest.mark.parametrize('value', ['0', 'false', 'no', '', 'maybe'])
def test_anything_else_leaves_creation_off(monkeypatch, value):
    """A typo in an env file must not arm a write to another team's board."""
    monkeypatch.setenv('BUG_CREATION_ENABLED', value)
    assert jc.creation_enabled() is False


# ---------------------------------------------------------------------------
# The return value: the bug this extraction fixes
# ---------------------------------------------------------------------------

def test_a_real_create_without_a_story_link_returns_the_key(monkeypatch):
    """The original returned None here, because the return was indented
    inside `if story_link:`. The issue existed in Jira, the review loop
    raised on issue.get('key'), caught it, and left the item queued for
    somebody to approve again - filing a duplicate."""
    api, _ = _live(monkeypatch)
    result = JiraClient().create_issue('a summary', 'a description')
    api.create_issue.assert_called_once()
    assert result['key'] == 'NC-123'
    assert result['dry_run'] is False


def test_a_real_create_with_a_story_link_returns_the_key(monkeypatch):
    api, issue = _live(monkeypatch)
    result = JiraClient().create_issue('a summary', 'a description',
                                       story_link='NC-99')
    assert result['key'] == 'NC-123'
    api.add_comment.assert_called_once_with(issue, 'Related story: NC-99')


def test_every_return_path_carries_a_key(monkeypatch):
    """A caller reads result['key']. There is no path where it is absent."""
    assert 'key' in JiraClient().create_issue('s', 'd')
    _live(monkeypatch)
    assert 'key' in JiraClient().create_issue('s', 'd')
    assert 'key' in JiraClient().create_issue('s', 'd', story_link='NC-1')


def test_a_failing_story_link_does_not_lose_the_issue_key(monkeypatch):
    """The issue exists. Losing its key because a comment failed would make
    the caller file it again."""
    api, _ = _live(monkeypatch)
    api.add_comment.side_effect = RuntimeError('comment rejected')
    result = JiraClient().create_issue('s', 'd', story_link='NC-99')
    assert result['key'] == 'NC-123'


def test_a_failing_attachment_does_not_lose_the_issue_key(monkeypatch, tmp_path):
    api, _ = _live(monkeypatch)
    api.add_attachment.side_effect = RuntimeError('too big')
    f = tmp_path / 'shot.png'
    f.write_bytes(b'x')
    assert JiraClient().create_issue('s', 'd', attachments=[str(f)])['key'] == 'NC-123'


def test_a_missing_attachment_is_skipped_not_raised(monkeypatch):
    api, _ = _live(monkeypatch)
    JiraClient().create_issue('s', 'd', attachments=['C:/nope/missing.png'])
    api.add_attachment.assert_not_called()


# ---------------------------------------------------------------------------
# Test bugs must be marked, with or without a story link
# ---------------------------------------------------------------------------

def test_a_test_issue_is_labelled_without_a_story_link(monkeypatch):
    """The second bug fixed here. The marking was indented inside
    `if story_link:` too, so a test bug filed without one carried nothing
    to say it was disposable."""
    api, issue = _live(monkeypatch)
    JiraClient().create_issue('[TEST] a summary', 'a description')
    api.add_comment.assert_called_once_with(issue, 'Admin: Please delete this test bug')
    issue.update.assert_called_once_with(fields={'labels': [TEST_LABEL]})


def test_a_test_issue_carries_the_delete_label_on_the_create(monkeypatch):
    """Belt and braces: the label goes on the create too, so an issue is
    never briefly unlabelled, and stays labelled if the update fails."""
    api, _ = _live(monkeypatch)
    JiraClient().create_issue('[TEST] a summary', 'd', labels=['automation'])
    sent = api.create_issue.call_args.kwargs['fields']
    assert sent['labels'] == ['automation', TEST_LABEL]


def test_a_normal_issue_is_not_labelled_as_a_test(monkeypatch):
    api, issue = _live(monkeypatch)
    JiraClient().create_issue('a real failure', 'd', labels=['automation'])
    assert api.create_issue.call_args.kwargs['fields']['labels'] == ['automation']
    issue.update.assert_not_called()


def test_the_dry_run_shows_the_test_label_too():
    """A dry run has to show what the real one would file, or it is not a
    rehearsal."""
    result = JiraClient().create_issue('[TEST] a summary', 'd')
    assert TEST_LABEL in result['labels']


def test_a_failing_label_update_does_not_lose_the_issue_key(monkeypatch):
    _, issue = _live(monkeypatch)
    issue.update.side_effect = RuntimeError('field locked')
    assert JiraClient().create_issue('[TEST] s', 'd')['key'] == 'NC-123'


# ---------------------------------------------------------------------------
# Field mapping through the client
# ---------------------------------------------------------------------------

def _mapper(mapped, unmapped=()):
    m = MagicMock()
    m.map_bug_fields.return_value = dict(mapped)
    m.get_unmapped_required_fields.return_value = list(unmapped)
    return m


def test_mapped_fields_ride_on_a_real_create(monkeypatch):
    api, _ = _live(monkeypatch)
    client = JiraClient(field_mapper=_mapper({'customfield_1': 'Views'}))
    client.create_issue('s', 'd', qa_touch_data={'module': 'Views'})
    assert api.create_issue.call_args.kwargs['fields']['customfield_1'] == 'Views'


def test_unmapped_required_fields_are_reported_not_swallowed(monkeypatch, caplog):
    """A tester who is not told meets a Jira validation error instead."""
    _live(monkeypatch)
    client = JiraClient(field_mapper=_mapper({}, ['Severity (customfield_9)']))
    with caplog.at_level(logging.WARNING, logger='soterix_qa_shared.jira'):
        result = client.create_issue('s', 'd', qa_touch_data={'module': 'Views'})
    assert result['unmapped_required_fields'] == ['Severity (customfield_9)']
    assert 'not auto-populated' in caplog.text


def test_the_dry_run_reports_unmapped_fields_too():
    client = JiraClient(field_mapper=_mapper({}, ['Severity (customfield_9)']))
    result = client.create_issue('s', 'd', qa_touch_data={'module': 'Views'})
    assert result['unmapped_required_fields'] == ['Severity (customfield_9)']


def test_no_qa_touch_data_maps_nothing(monkeypatch):
    api, _ = _live(monkeypatch)
    m = _mapper({'customfield_1': 'x'})
    JiraClient(field_mapper=m).create_issue('s', 'd')
    m.map_bug_fields.assert_not_called()
    assert 'customfield_1' not in api.create_issue.call_args.kwargs['fields']


def test_fields_are_mapped_only_for_bugs(monkeypatch):
    """The mapping is Bug-specific. Sending a Bug's custom fields on a Task
    is how a create gets rejected for an unknown field."""
    api, _ = _live(monkeypatch)
    m = _mapper({'customfield_1': 'x'})
    JiraClient(field_mapper=m).create_issue('s', 'd', issue_type='Task',
                                            qa_touch_data={'module': 'Views'})
    m.map_bug_fields.assert_not_called()
    assert api.create_issue.call_args.kwargs['fields']['issuetype'] == {'name': 'Task'}


def test_the_project_key_comes_from_the_environment_when_not_given(monkeypatch):
    api, _ = _live(monkeypatch)
    monkeypatch.setenv('JIRA_PROJECT_KEY', 'NC')
    JiraClient().create_issue('s', 'd')
    assert api.create_issue.call_args.kwargs['fields']['project'] == {'key': 'NC'}


def test_an_explicit_project_key_wins(monkeypatch):
    api, _ = _live(monkeypatch)
    monkeypatch.setenv('JIRA_PROJECT_KEY', 'NC')
    JiraClient().create_issue('s', 'd', project_key='OTHER')
    assert api.create_issue.call_args.kwargs['fields']['project'] == {'key': 'OTHER'}
