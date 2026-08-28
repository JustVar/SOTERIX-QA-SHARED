"""Node-id results, the sink interface, and the local run record.

These pin the identity decision behind S5: a test result is keyed by node
id, and folding to QA Touch case keys is one sink's private step.
"""
from __future__ import annotations

import json

from soterix_qa_shared.qatouch.artifacts import (
    RUN_RESULT_RECORD_FIELDS,
    RUN_RESULTS_FIELDS,
)
from soterix_qa_shared.qatouch.results import (
    RunResult,
    RunResultsFileSink,
    collapse_to_cases,
    merge_reruns,
    worse_outcome,
)
from soterix_qa_shared.qatouch.upload import (
    PYTEST_TO_QT,
    STATUS_BLOCKED,
    STATUS_FAILED,
    STATUS_PASSED,
)


def _r(nodeid, outcome, **kw):
    return RunResult(nodeid=nodeid, outcome=outcome, **kw)


# ---------------------------------------------------------------------------
# Outcome ordering
# ---------------------------------------------------------------------------

def test_worse_outcome_orders_failed_above_skipped_above_passed():
    assert worse_outcome('failed', 'skipped')
    assert worse_outcome('skipped', 'passed')
    assert not worse_outcome('passed', 'failed')
    assert not worse_outcome('failed', 'failed')  # equal is not worse


def test_worse_outcome_treats_an_unknown_outcome_as_lowest():
    """A new pytest outcome must not silently outrank a real failure."""
    assert not worse_outcome('rerun', 'failed')
    assert worse_outcome('passed', 'rerun')


def test_base_nodeid_drops_the_parametrization():
    assert _r('t.py::test_a[chromium]', 'passed').base_nodeid == 't.py::test_a'
    assert _r('t.py::test_a', 'passed').base_nodeid == 't.py::test_a'


# ---------------------------------------------------------------------------
# Reruns
# ---------------------------------------------------------------------------

def test_merge_reruns_keeps_the_worse_report():
    first = _r('t::a', 'passed', duration=0.1)
    second = _r('t::a', 'failed', duration=9.9)
    assert merge_reruns(first, second) is second
    assert merge_reruns(second, first) is second


def test_merge_reruns_keeps_the_failures_timing_not_the_last_one():
    """The pre-split reporter took the LAST duration, so a case could upload
    "failed" beside the passing rerun's timing."""
    failed = _r('t::a', 'failed', duration=9.9, screenshot=b'png')
    passed_rerun = _r('t::a', 'passed', duration=0.1)
    merged = merge_reruns(failed, passed_rerun)
    assert merged.duration == 9.9
    assert merged.screenshot == b'png'


# ---------------------------------------------------------------------------
# Folding node-id results into cases
# ---------------------------------------------------------------------------

def test_collapse_skips_unmapped_results():
    """An unmapped test is still recorded, but it has no case to upload to."""
    folded = collapse_to_cases([_r('t::a', 'failed')], PYTEST_TO_QT)
    assert folded['statuses'] == {}


def test_collapse_skips_an_outcome_qatouch_has_no_status_for():
    folded = collapse_to_cases([_r('t::a', 'xfailed', case_key='K1')], PYTEST_TO_QT)
    assert folded['statuses'] == {}


def test_two_tests_on_one_case_upload_the_worst_status():
    """42 case keys in the UI mapping are claimed by more than one node id.

    Keyed by case key, whichever ran last used to win. Keyed by node id,
    both are recorded and the fold applies the same worst-wins rule reruns
    already follow.
    """
    folded = collapse_to_cases(
        [_r('t::a', 'passed', case_key='K1', duration=0.1),
         _r('t::b', 'failed', case_key='K1', duration=2.0, screenshot=b'png')],
        PYTEST_TO_QT)
    assert folded['statuses'] == {'K1': STATUS_FAILED}
    assert sorted(folded['nodeids']['K1']) == ['t::a', 't::b']


def test_a_later_pass_does_not_erase_an_earlier_failure_on_a_shared_case():
    """The order that discriminates.

    With [passed, failed] a last-wins implementation and a worst-wins one
    agree, so that ordering proves nothing. Reversed, last-wins uploads
    "passed" over a real failure - silently, on a case key two tests share.
    That is the bug this whole re-keying exists to remove, so it gets the
    ordering that can actually see it.
    """
    folded = collapse_to_cases(
        [_r('t::fails', 'failed', case_key='K1'),
         _r('t::passes', 'passed', case_key='K1')],
        PYTEST_TO_QT)
    assert folded['statuses'] == {'K1': STATUS_FAILED}


def test_a_skip_does_not_erase_a_failure_on_a_shared_case():
    """Blocked outranks passed but not failed."""
    folded = collapse_to_cases(
        [_r('t::fails', 'failed', case_key='K1'),
         _r('t::skips', 'skipped', case_key='K1')],
        PYTEST_TO_QT)
    assert folded['statuses'] == {'K1': STATUS_FAILED}


def test_a_skip_does_outrank_a_pass_on_a_shared_case():
    folded = collapse_to_cases(
        [_r('t::skips', 'skipped', case_key='K1'),
         _r('t::passes', 'passed', case_key='K1')],
        PYTEST_TO_QT)
    assert folded['statuses'] == {'K1': STATUS_BLOCKED}


def test_the_evidence_comes_from_the_test_that_reported_worst():
    """A screenshot that does not belong to the uploaded status is worse
    than no screenshot - it sends a reader to the wrong test."""
    folded = collapse_to_cases(
        [_r('t::fails', 'failed', case_key='K1', duration=2.0, screenshot=b'the-failure'),
         _r('t::passes', 'passed', case_key='K1', duration=0.1, screenshot=b'not-it')],
        PYTEST_TO_QT)
    assert folded['screenshots']['K1'] == b'the-failure'
    assert folded['durations']['K1'] == 2.0


def test_the_evidence_survives_a_reversed_arrival_order():
    """Order of arrival must not decide which evidence is uploaded."""
    folded = collapse_to_cases(
        [_r('t::passes', 'passed', case_key='K1', duration=0.1, screenshot=b'not-it'),
         _r('t::fails', 'failed', case_key='K1', duration=2.0, screenshot=b'the-failure')],
        PYTEST_TO_QT)
    assert folded['screenshots']['K1'] == b'the-failure'
    assert folded['durations']['K1'] == 2.0


def test_collapse_records_the_project_each_case_resolved_to():
    folded = collapse_to_cases(
        [_r('t::a', 'passed', case_key='K1', project_key='JlW1'),
         _r('t::b', 'passed', case_key='K2', project_key='60W5')],
        PYTEST_TO_QT)
    assert folded['projects'] == {'K1': 'JlW1', 'K2': '60W5'}


def test_a_case_covering_one_test_reports_one_nodeid():
    folded = collapse_to_cases([_r('t::a', 'passed', case_key='K1')], PYTEST_TO_QT)
    assert folded['nodeids'] == {'K1': ['t::a']}
    assert folded['statuses'] == {'K1': STATUS_PASSED}


# ---------------------------------------------------------------------------
# The local run record
# ---------------------------------------------------------------------------

def test_run_record_matches_the_frozen_shape(tmp_path):
    """run_archive, run_report, run_suite and validate_run all read this."""
    path = tmp_path / 'logs' / 'last_run_results.json'
    sink = RunResultsFileSink(path, run_key='RUN1', project_key='JlW1')
    sink.finish([_r('t::a', 'failed', duration=1.5, case_key='K1')])
    payload = json.loads(path.read_text())
    assert tuple(payload) == RUN_RESULTS_FIELDS
    assert tuple(payload['results'][0]) == RUN_RESULT_RECORD_FIELDS


def test_run_record_is_written_without_a_run_key(tmp_path):
    """The gap this split closes: an unarmed run used to write nothing at
    all, so the flaky history and the run cross-check saw no runs."""
    path = tmp_path / 'logs' / 'last_run_results.json'
    sink = RunResultsFileSink(path)
    sink.finish([_r('t::a', 'passed')])
    payload = json.loads(path.read_text())
    assert payload['armed'] is False
    assert payload['run_key'] is None
    assert payload['results'][0]['nodeid'] == 't::a'


def test_run_record_marks_an_armed_session(tmp_path):
    path = tmp_path / 'logs' / 'last_run_results.json'
    sink = RunResultsFileSink(path, run_key='RUN1')
    sink.finish([])
    assert json.loads(path.read_text())['armed'] is True


def test_run_record_includes_unmapped_tests(tmp_path):
    """An unmapped failure still deserves a bug, so it must be in the record."""
    path = tmp_path / 'logs' / 'last_run_results.json'
    sink = RunResultsFileSink(path)
    sink.finish([_r('t::mapped', 'passed', case_key='K1'),
                 _r('t::unmapped', 'failed')])
    rows = json.loads(path.read_text())['results']
    assert {r['nodeid']: r['case_key'] for r in rows} == {
        't::mapped': 'K1', 't::unmapped': None}


def test_run_record_write_failure_never_raises(tmp_path):
    """Rule 7: a local write must degrade, never kill the run."""
    sink = RunResultsFileSink(tmp_path / 'logs\x00bad' / 'x.json')
    sink.finish([_r('t::a', 'passed')])   # no raise
    assert sink.error is not None
    assert 'NOT written' in sink.summary()[0]
