"""The QA Touch upload sink.

Ported from the UI repo's reporter suite. Every assertion about talking to
QA Touch moved here unchanged in meaning; only the setup changed, because
the sink now receives node-id results instead of a pre-folded case map.

Pins for the 2026-08-17 silent-no-op work: the results API accepts a case
key or a per-run result key, answers {"success": true}, and writes nothing.
Only the case CODE lands. These tests make sure the sink resolves codes,
never sends a form the API ignores, and reads the run back after every
upload.
"""
from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock

import pytest

import soterix_qa_shared.qatouch as qatouch_pkg
from soterix_qa_shared.qatouch import upload as up
from soterix_qa_shared.qatouch.results import RunResult
from soterix_qa_shared.qatouch.upload import QATouchUploadSink


def _r(nodeid, outcome, **kw):
    return RunResult(nodeid=nodeid, outcome=outcome, **kw)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """The 429 backoff sleeps 30s per attempt; a unit test must not."""
    monkeypatch.setattr(up.time, 'sleep', lambda _s: None)


def _sink(tmp_path, **kw):
    kw.setdefault('project_key', 'JlW1')
    kw.setdefault('failure_log_path', tmp_path / 'logs' / 'upload_failures.json')
    return QATouchUploadSink(run_key='RUN1', **kw)


def _fake_client(monkeypatch, **attrs):
    """Install a fake QATouchClient on the package the sink imports from."""
    client = MagicMock(**attrs)
    monkeypatch.setattr(qatouch_pkg, 'QATouchClient', MagicMock(return_value=client))
    return client


# ---------------------------------------------------------------------------
# Duration formatting
# ---------------------------------------------------------------------------

def test_format_duration_matches_the_vendor_reporter():
    assert up.format_duration(1.23) == '1.2s'
    assert up.format_duration(0.45) == '450ms'
    assert up.format_duration(None) == ''
    assert up.format_duration(-1) == ''


# ---------------------------------------------------------------------------
# Case-code resolution
# ---------------------------------------------------------------------------

def test_case_code_mapping_resolves_codes_not_result_keys(tmp_path):
    """The mapping stores case codes, never the per-run result keys."""
    s = _sink(tmp_path)
    client = MagicMock()
    client.get_all_test_run_results.return_value = [
        {'case_id': 'B869bm', 'run_key': 'w5BNkb', 'code': 'TR0262', 'status': 'Untested'},
        {'case_id': '8V1vw1', 'run_key': 'y5MNkx', 'code': 'TR0263', 'status': 'Untested'},
    ]
    s._build_case_code_mapping(client)
    assert s.case_to_code == {'B869bm': 'TR0262', '8V1vw1': 'TR0263'}
    assert 'w5BNkb' not in s.case_to_code.values()


def test_build_payload_uses_codes_and_skips_unresolved(tmp_path):
    """The payload carries case codes and drops cases with no code."""
    s = _sink(tmp_path)
    s.statuses = {'B869bm': 1, '8V1vw1': 5, 'NOMAP1': 1}
    s.case_to_code = {'B869bm': 'TR0262', '8V1vw1': 'TR0263'}
    payload = s._build_payload(['B869bm', '8V1vw1', 'NOMAP1'])
    assert payload == [{'case': 'TR0262', 'status': 1}, {'case': 'TR0263', 'status': 5}]
    # An unresolved case must be dropped, not sent as a key the API ignores.
    assert all(e['case'].startswith('TR') for e in payload)


# ---------------------------------------------------------------------------
# Per-project grouping
# ---------------------------------------------------------------------------

def test_group_by_project_puts_unmarked_cases_under_the_default(tmp_path):
    s = _sink(tmp_path)
    s.statuses = {'C1': 1, 'C2': 5}
    groups = s._group_by_project()
    assert set(groups) == {'JlW1'}
    assert sorted(groups['JlW1']) == ['C1', 'C2']


def test_group_by_project_splits_by_resolved_project(tmp_path):
    s = _sink(tmp_path)
    s.statuses = {'C1': 1, 'C2': 5, 'C3': 1}
    s.case_projects = {'C1': 'JlW1', 'C2': 'OTHER', 'C3': 'JlW1'}
    groups = s._group_by_project()
    assert set(groups) == {'JlW1', 'OTHER'}
    assert sorted(groups['JlW1']) == ['C1', 'C3']
    assert groups['OTHER'] == ['C2']


def test_a_case_for_another_project_is_skipped_loudly(monkeypatch, tmp_path, caplog):
    """One run key belongs to one project. Cases pointing elsewhere have no
    run to land in, so they are dropped and counted as an error."""
    _fake_client(monkeypatch)
    s = _sink(tmp_path)
    with caplog.at_level(logging.ERROR, logger='qatouch_reporter'):
        s.finish([_r('t::a', 'passed', case_key='C1', project_key='60W5')], None)
    assert 'no per-project run key' in caplog.text
    assert s.stats['errors'] == 1
    assert s.stats['uploaded'] == 0


# ---------------------------------------------------------------------------
# Read-back
# ---------------------------------------------------------------------------

def test_verify_fails_on_a_readback_mismatch(tmp_path):
    s = _sink(tmp_path)
    client = MagicMock()
    client.verify_results_landed.return_value = {
        'checked': 2, 'landed': ['TR0262'],
        'mismatched': ['TR0263: sent Failed, run shows Untested'], 'missing': []}
    ok = s._verify_upload(client, [{'case': 'TR0262', 'status': 1},
                                   {'case': 'TR0263', 'status': 5}])
    assert ok is False
    client.verify_results_landed.assert_called_once_with(
        'JlW1', 'RUN1', [{'case': 'TR0262', 'status': 1}, {'case': 'TR0263', 'status': 5}])


def test_verify_passes_when_everything_landed(tmp_path):
    s = _sink(tmp_path)
    client = MagicMock()
    client.verify_results_landed.return_value = {
        'checked': 1, 'landed': ['TR0262'], 'mismatched': [], 'missing': []}
    assert s._verify_upload(client, [{'case': 'TR0262', 'status': 1}]) is True


def test_verify_does_not_fail_the_run_on_a_readback_error(tmp_path):
    """A read-back problem is not evidence the upload failed - warn, do not
    block. A 429 is retried and then conceded."""
    s = _sink(tmp_path)
    client = MagicMock()
    client.verify_results_landed.side_effect = RuntimeError('429')
    assert s._verify_upload(client, [{'case': 'TR0262', 'status': 1}]) is True
    assert client.verify_results_landed.call_count == 3


def test_client_has_no_update_playwright_results():
    """The playwright branch used to call a method that never existed."""
    from soterix_qa_shared.qatouch import QATouchClient
    assert not hasattr(QATouchClient, 'update_playwright_results')
    assert hasattr(QATouchClient, 'update_test_run_results_by_code')


# ---------------------------------------------------------------------------
# "No case codes resolved" is a FAILURE, not an empty success (M1, 2026-08-20)
# ---------------------------------------------------------------------------

def test_unresolvable_codes_fail_loud_and_land_in_the_retry_log(monkeypatch, tmp_path):
    fake_client = _fake_client(monkeypatch)
    fake_client.get_all_test_run_results.side_effect = RuntimeError('HTTP 429')

    s = _sink(tmp_path)
    s.finish([_r('t::a', 'passed', case_key='B869bm'),
              _r('t::b', 'failed', case_key='8V1vw1')], None)

    # Nothing sent in a form the API would ignore, and nothing "succeeded".
    fake_client.update_test_run_results_by_code.assert_not_called()
    assert s.stats['errors'] == 1
    assert s.stats['uploaded'] == 0

    saved = json.loads((tmp_path / 'logs' / 'upload_failures.json').read_text())
    assert len(saved) == 1
    assert {e['case'] for e in saved[0]['results']} == {'B869bm', '8V1vw1'}
    assert 'No case codes resolved' in saved[0]['error']


def test_uploads_normally_when_codes_resolve(monkeypatch, tmp_path):
    fake_client = _fake_client(monkeypatch)
    fake_client.get_all_test_run_results.return_value = [
        {'case_id': 'B869bm', 'code': 'TR0262', 'status': 'Untested'}]
    fake_client.verify_results_landed.return_value = {
        'checked': 1, 'landed': ['TR0262'], 'mismatched': [], 'missing': []}

    s = _sink(tmp_path)
    s.finish([_r('t::a', 'passed', case_key='B869bm')], None)

    fake_client.update_test_run_results_by_code.assert_called_once()
    sent = fake_client.update_test_run_results_by_code.call_args.kwargs['results']
    assert sent == [{'case': 'TR0262', 'status': 1}]
    assert s.stats['uploaded'] == 1
    assert s.stats['errors'] == 0
    assert not (tmp_path / 'logs' / 'upload_failures.json').exists()


def test_a_run_with_no_mapped_results_uploads_nothing(monkeypatch, tmp_path):
    """An unmapped-only run must not reach the client at all."""
    fake_client = _fake_client(monkeypatch)
    s = _sink(tmp_path)
    s.finish([_r('t::a', 'failed')], None)
    fake_client.update_test_run_results_by_code.assert_not_called()
    assert s.stats['errors'] == 0


# ---------------------------------------------------------------------------
# Attachments: failure screenshots and timing ride the result upload as
# multipart file[] + comments.
# ---------------------------------------------------------------------------

def _playwright_sink(monkeypatch, tmp_path):
    fake_client = _fake_client(monkeypatch)
    fake_client.get_all_test_run_results.return_value = [
        {'case_id': 'B869bm', 'code': 'TR0262', 'status': 'Untested'}]
    fake_client.verify_results_landed.return_value = {
        'checked': 1, 'landed': ['TR0262'], 'mismatched': [], 'missing': []}
    return _sink(tmp_path, use_playwright_endpoint=True), fake_client


def test_attaches_screenshot_and_timing(monkeypatch, tmp_path):
    """A failed case uploads with its screenshot named by code and a timing line."""
    s, fake_client = _playwright_sink(monkeypatch, tmp_path)
    s.finish([_r('t::a', 'failed', case_key='B869bm', duration=1.23,
                 screenshot=b'\x89PNG-bytes')], None)

    fake_client.update_test_run_results_by_code.assert_called_once()
    kwargs = fake_client.update_test_run_results_by_code.call_args.kwargs
    assert kwargs['results'] == [{'case': 'TR0262', 'status': 5}]
    # File named by case CODE so a reader of the run can match it.
    assert kwargs['attachments'] == [('TR0262-failure.png', b'\x89PNG-bytes', 'image/png')]
    assert 'TR0262 execution: 1.2s' in kwargs['comments']
    assert s.stats['uploaded'] == 1
    assert s.stats['errors'] == 0


def test_skips_an_oversized_screenshot_whole(monkeypatch, tmp_path, caplog):
    """Over 2 MB is skipped whole - a cut PNG uploads fine and opens broken."""
    s, fake_client = _playwright_sink(monkeypatch, tmp_path)
    with caplog.at_level(logging.WARNING, logger='qatouch_reporter'):
        s.finish([_r('t::a', 'failed', case_key='B869bm',
                     screenshot=b'x' * (up.MAX_ATTACHMENT_BYTES + 1))], None)

    assert 'over the 2 MB attachment cap' in caplog.text
    fake_client.update_test_run_results_by_code.assert_called_once()
    kwargs = fake_client.update_test_run_results_by_code.call_args.kwargs
    assert 'attachments' not in kwargs
    assert 'execution' not in kwargs['comments']
    assert s.stats['uploaded'] == 1


def test_without_screenshots_the_request_is_byte_compatible(monkeypatch, tmp_path):
    """Timing rides only with attachments, so a run with durations but no
    screenshots produces the same request as before the feature."""
    s, fake_client = _playwright_sink(monkeypatch, tmp_path)
    s.finish([_r('t::a', 'passed', case_key='B869bm', duration=0.45)], None)

    fake_client.update_test_run_results_by_code.assert_called_once()
    kwargs = fake_client.update_test_run_results_by_code.call_args.kwargs
    assert 'attachments' not in kwargs
    assert kwargs['comments'].startswith('Automated upload from Playwright run at')
    assert 'execution' not in kwargs['comments']


def test_a_rejected_multipart_degrades_to_the_plain_request(monkeypatch, tmp_path):
    """Statuses are never hostage to a screenshot."""
    s, fake_client = _playwright_sink(monkeypatch, tmp_path)
    calls = []

    def fake_update(**kwargs):
        calls.append(kwargs)
        if kwargs.get('attachments'):
            raise RuntimeError('multipart rejected')
        return {'success': True}

    fake_client.update_test_run_results_by_code.side_effect = fake_update
    s.finish([_r('t::a', 'failed', case_key='B869bm', screenshot=b'\x89PNG-bytes')], None)

    assert len(calls) == 2
    assert calls[0].get('attachments')
    assert 'attachments' not in calls[1]
    assert calls[1]['results'] == [{'case': 'TR0262', 'status': 5}]
    assert calls[1]['comments'].startswith('Automated upload from Playwright run at')
    assert s.stats['uploaded'] == 1
    assert s.stats['errors'] == 0


def test_attachment_parts_skip_unresolved_and_non_failed(tmp_path):
    """No file for a case whose result was dropped (no code) or is not
    FAILED - an attachment with no uploaded result has nothing to hang on."""
    s = _sink(tmp_path)
    s.statuses = {'B869bm': up.STATUS_FAILED, '8V1vw1': up.STATUS_PASSED,
                  'NOMAP1': up.STATUS_FAILED}
    s.screenshots = {'B869bm': b'ok', '8V1vw1': b'passed-anyway', 'NOMAP1': b'no-code'}
    s.case_to_code = {'B869bm': 'TR0262', '8V1vw1': 'TR0263'}
    parts = s._collect_attachment_parts(['B869bm', '8V1vw1', 'NOMAP1'])
    assert parts == {'B869bm': ('TR0262-failure.png', b'ok', 'image/png')}


def test_timing_lines_cover_only_the_attached_cases(monkeypatch, tmp_path):
    """The comment rides the URL query string, so a line per uploaded case
    would grow the URL with run size - the part of the feature most likely
    to be rejected on exactly the large runs it targets."""
    fake_client = _fake_client(monkeypatch)
    fake_client.get_all_test_run_results.return_value = [
        {'case_id': 'B869bm', 'code': 'TR0262', 'status': 'Untested'},
        {'case_id': '8V1vw1', 'code': 'TR0263', 'status': 'Untested'},
    ]
    fake_client.verify_results_landed.return_value = {
        'checked': 2, 'landed': ['TR0262', 'TR0263'], 'mismatched': [], 'missing': []}

    s = _sink(tmp_path, use_playwright_endpoint=True)
    s.finish([_r('t::a', 'failed', case_key='B869bm', duration=1.23,
                 screenshot=b'\x89PNG-bytes'),
              _r('t::b', 'passed', case_key='8V1vw1', duration=0.45)], None)

    kwargs = fake_client.update_test_run_results_by_code.call_args.kwargs
    assert 'TR0262 execution: 1.2s' in kwargs['comments']
    # The passed case uploaded a status but no attachment - no timing line.
    assert 'TR0263' not in kwargs['comments']
    assert {e['case'] for e in kwargs['results']} == {'TR0262', 'TR0263'}


def test_accepted_but_ignored_multipart_degrades_to_plain(monkeypatch, tmp_path):
    """The API's measured second failure class: the multipart request
    answers {"success": true} but the statuses never apply. The read-back
    catches it, and the sink re-sends the plain request in-session instead
    of conceding to the failure log."""
    s, fake_client = _playwright_sink(monkeypatch, tmp_path)
    calls = []

    def fake_update(**kwargs):
        calls.append(kwargs)
        return {'success': True, 'msg': 'TR0262 - Updated Successfully'}

    fake_client.update_test_run_results_by_code.side_effect = fake_update
    fake_client.verify_results_landed.side_effect = [
        {'checked': 1, 'landed': [],
         'mismatched': ['TR0262: sent Failed, run shows Untested'], 'missing': []},
        {'checked': 1, 'landed': ['TR0262'], 'mismatched': [], 'missing': []},
    ]
    s.finish([_r('t::a', 'failed', case_key='B869bm', screenshot=b'\x89PNG-bytes')], None)

    assert len(calls) == 2
    assert calls[0].get('attachments')
    assert 'attachments' not in calls[1]
    assert calls[1]['results'] == [{'case': 'TR0262', 'status': 5}]
    assert s.stats['uploaded'] == 1
    assert s.stats['errors'] == 0
    assert not (tmp_path / 'logs' / 'upload_failures.json').exists()


def test_a_plain_verify_failure_saves_for_retry_without_respinning(monkeypatch, tmp_path):
    """When the plain request itself does not land (no attachments were
    sent), the failure goes to the log - one plain send, no re-send."""
    s, fake_client = _playwright_sink(monkeypatch, tmp_path)
    fake_client.verify_results_landed.return_value = {
        'checked': 1, 'landed': [],
        'mismatched': ['TR0262: sent Passed, run shows Untested'], 'missing': []}
    s.finish([_r('t::a', 'passed', case_key='B869bm')], None)   # no screenshot

    fake_client.update_test_run_results_by_code.assert_called_once()
    assert s.stats['errors'] == 1
    assert (tmp_path / 'logs' / 'upload_failures.json').exists()


# ---------------------------------------------------------------------------
# The retry log
# ---------------------------------------------------------------------------

def test_failure_entry_matches_the_frozen_shape(tmp_path):
    from soterix_qa_shared.qatouch.artifacts import (
        FAILURE_ENTRY_FIELDS,
        FAILURE_RESULT_FIELDS,
    )
    s = _sink(tmp_path)
    s._save_failed_results([{'case': 'TR0262', 'status': 5}], 'a comment', 'an error')
    saved = json.loads((tmp_path / 'logs' / 'upload_failures.json').read_text())
    assert tuple(saved[0]) == FAILURE_ENTRY_FIELDS
    assert tuple(saved[0]['results'][0]) == FAILURE_RESULT_FIELDS


def test_failure_log_appends_rather_than_overwrites(tmp_path):
    """Two failed sessions must not lose the first session's results."""
    s = _sink(tmp_path)
    s._save_failed_results([{'case': 'TR0262', 'status': 5}], 'c1', 'e1')
    s._save_failed_results([{'case': 'TR0263', 'status': 1}], 'c2', 'e2')
    saved = json.loads((tmp_path / 'logs' / 'upload_failures.json').read_text())
    assert [e['results'][0]['case'] for e in saved] == ['TR0262', 'TR0263']


def test_a_malformed_failure_log_does_not_lose_the_new_entry(tmp_path):
    (tmp_path / 'logs').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'logs' / 'upload_failures.json').write_text('not json', encoding='utf-8')
    s = _sink(tmp_path)
    s._save_failed_results([{'case': 'TR0262', 'status': 5}], 'c', 'e')
    assert len(json.loads((tmp_path / 'logs' / 'upload_failures.json').read_text())) == 1


def test_an_unwritable_failure_log_does_not_raise(tmp_path):
    s = _sink(tmp_path, failure_log_path=tmp_path / 'logs\x00bad' / 'x.json')
    s._save_failed_results([{'case': 'TR0262', 'status': 5}], 'c', 'e')   # no raise


def test_no_failure_log_path_says_so_rather_than_dropping_results(tmp_path, caplog):
    """Losing results silently is the failure class this sink exists to stop."""
    s = QATouchUploadSink(run_key='RUN1', failure_log_path=None)
    with caplog.at_level(logging.ERROR, logger='qatouch_reporter'):
        s._save_failed_results([{'case': 'TR0262', 'status': 5}], 'c', 'e')
    assert 'could not be saved for retry' in caplog.text


# ---------------------------------------------------------------------------
# Terminal summary
# ---------------------------------------------------------------------------

def test_summary_names_the_cases_that_cover_more_than_one_test(tmp_path):
    """One uploaded status standing for several tests is otherwise invisible."""
    s = _sink(tmp_path)
    s.case_nodeids = {'K1': ['t::a', 't::b'], 'K2': ['t::c']}
    text = '\n'.join(s.summary())
    assert '1 case(s) cover more than one test' in text


def test_summary_says_nothing_about_sharing_when_nothing_is_shared(tmp_path):
    s = _sink(tmp_path)
    s.case_nodeids = {'K1': ['t::a']}
    assert 'cover more than one test' not in '\n'.join(s.summary())
