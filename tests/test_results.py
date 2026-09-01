"""Node-id results, the sink interface, and the local run record.

These pin the identity decision behind S5: a test result is keyed by node
id, and folding to QA Touch case keys is one sink's private step.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import UTC, datetime, timedelta

from soterix_qa_shared.qatouch.artifacts import (
    RUN_RESULT_RECORD_FIELDS,
    RUN_RESULTS_FIELDS,
)
from soterix_qa_shared.qatouch.results import (
    RunContext,
    RunLedgerSink,
    RunResult,
    RunResultsFileSink,
    collapse_to_cases,
    merge_reruns,
    relative_target,
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


def _ctx(**kw):
    started = kw.pop('started', datetime(2026, 8, 28, 14, 2, tzinfo=UTC))
    ended = kw.pop('ended', started + timedelta(seconds=1840))
    return RunContext(started=started, ended=ended, **kw)


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
    sink = RunResultsFileSink(path)
    sink.finish([_r('t::a', 'failed', duration=1.5, case_key='K1')],
                _ctx(run_key='RUN1', project_key='JlW1'))
    payload = json.loads(path.read_text())
    assert tuple(payload) == RUN_RESULTS_FIELDS
    assert tuple(payload['results'][0]) == RUN_RESULT_RECORD_FIELDS


def test_the_run_record_carries_the_exit_status(tmp_path):
    """A run stopped by -x or Ctrl-C is otherwise indistinguishable on disk
    from a smaller suite that finished cleanly."""
    path = tmp_path / 'logs' / 'last_run_results.json'
    RunResultsFileSink(path).finish([_r('t::a', 'passed')], _ctx(exitstatus=2))
    assert json.loads(path.read_text())['exit'] == 2


def test_run_record_is_written_without_a_run_key(tmp_path):
    """The gap this split closes: an unarmed run used to write nothing at
    all, so the flaky history and the run cross-check saw no runs."""
    path = tmp_path / 'logs' / 'last_run_results.json'
    sink = RunResultsFileSink(path)
    sink.finish([_r('t::a', 'passed')], _ctx())
    payload = json.loads(path.read_text())
    assert payload['armed'] is False
    assert payload['run_key'] is None
    assert payload['results'][0]['nodeid'] == 't::a'


def test_run_record_marks_an_armed_session(tmp_path):
    path = tmp_path / 'logs' / 'last_run_results.json'
    sink = RunResultsFileSink(path)
    sink.finish([_r('t::a', 'passed')], _ctx(run_key='RUN1'))
    assert json.loads(path.read_text())['armed'] is True


def test_a_session_that_recorded_nothing_leaves_the_record_alone(tmp_path):
    """Measured across 21 invocations: -x, --maxfail, a -k or -m matching
    nothing, --deselect, a collection error, a mistyped node id, a
    nonexistent path, --fixtures and Ctrl-C all reach finish() with an
    empty list. Each one used to blank a real record, and every consumer
    reads an empty file as "a run with no failures" rather than "no run"."""
    path = tmp_path / 'logs' / 'last_run_results.json'
    sink = RunResultsFileSink(path)
    sink.finish([_r('t::a', 'passed'), _r('t::b', 'failed')], _ctx())
    before = path.read_text()

    RunResultsFileSink(path).finish([], _ctx())
    assert path.read_text() == before

    later = RunResultsFileSink(path)
    later.finish([], _ctx())
    assert later.skipped_empty is True
    assert 'unchanged' in later.summary()[0]


def test_an_armed_session_that_ran_nothing_appends_no_ledger_row(tmp_path):
    """The ledger is append-only, so a row saying counts={} failed=[] can
    never be taken back, and no later reader can tell it from a real run
    where everything was deselected."""
    sink = RunLedgerSink(tmp_path / 'history')
    sink.finish([], _ctx(run_key='d6pw'))
    assert not (tmp_path / 'history').exists()


def test_run_record_includes_unmapped_tests(tmp_path):
    """An unmapped failure still deserves a bug, so it must be in the record."""
    path = tmp_path / 'logs' / 'last_run_results.json'
    sink = RunResultsFileSink(path)
    sink.finish([_r('t::mapped', 'passed', case_key='K1'),
                 _r('t::unmapped', 'failed')], _ctx())
    rows = json.loads(path.read_text())['results']
    assert {r['nodeid']: r['case_key'] for r in rows} == {
        't::mapped': 'K1', 't::unmapped': None}


def test_run_record_write_failure_never_raises(tmp_path):
    """Rule 7: a local write must degrade, never kill the run."""
    sink = RunResultsFileSink(tmp_path / 'logs\x00bad' / 'x.json')
    sink.finish([_r('t::a', 'passed')], _ctx())   # no raise
    assert sink.error is not None
    assert 'NOT written' in sink.summary()[0]


# ---------------------------------------------------------------------------
# Targets, made comparable across machines
# ---------------------------------------------------------------------------

def test_an_absolute_target_is_stored_relative_to_the_root(tmp_path):
    """Measured on a real armed run: the ledger recorded
    'C:/Users/<name>/Soterix/qa/Soterix-API-QA/tests'. The file is committed
    and read by other people, so that row named a directory none of them
    have and carried a home directory into a shared repo."""
    (tmp_path / 'tests').mkdir()
    assert relative_target([str(tmp_path / 'tests')], tmp_path) == ('tests',)


def test_a_relative_target_is_left_alone(tmp_path):
    (tmp_path / 'tests').mkdir()
    assert relative_target(['tests'], tmp_path) == ('tests',)


def test_two_spellings_of_one_run_record_the_same_target(tmp_path):
    """The point of the whole function: the ledger groups by target, so the
    same suite run two ways has to produce one string."""
    (tmp_path / 'tests').mkdir()
    assert (relative_target([str(tmp_path / 'tests')], tmp_path)
            == relative_target(['tests'], tmp_path))


def test_a_node_id_keeps_its_test_name(tmp_path):
    """The :: part is the identity the rest of the pipeline uses."""
    (tmp_path / 'tests').mkdir()
    (tmp_path / 'tests' / 'test_a.py').write_text('', encoding='utf-8')
    arg = str(tmp_path / 'tests' / 'test_a.py') + '::TestX::test_y[chromium]'
    assert relative_target([arg], tmp_path) == (
        'tests/test_a.py::TestX::test_y[chromium]',)


def test_a_path_outside_the_root_is_passed_through(tmp_path):
    """Guessing at it would be worse than recording what was asked for."""
    other = tmp_path.parent / 'somewhere-else'
    assert relative_target([str(other)], tmp_path) == (str(other),)


def test_a_non_path_argument_is_passed_through(tmp_path):
    assert relative_target(['not-a-path-expression'], tmp_path) == (
        'not-a-path-expression',)


def test_no_targets_stays_empty(tmp_path):
    """Bare pytest. The ledger reads an empty target as 'the whole repo'."""
    assert relative_target([], tmp_path) == ()
def test_windows_separators_are_recorded_as_posix(tmp_path):
    """A native separator would make the same run read one way on
    Windows and another on Linux, and no single jq query would find
    both. os.sep is used rather than a literal so this test says what it
    means on either platform."""
    (tmp_path / 'tests').mkdir()
    (tmp_path / 'tests' / 'ui').mkdir()
    recorded = relative_target([str(tmp_path / 'tests' / 'ui')], tmp_path)[0]
    assert recorded == 'tests/ui'
    if os.sep != '/':
        assert os.sep not in recorded


# ---------------------------------------------------------------------------
# The tracked run ledger
#
# reports/ held 4.4 GB after a week of runs and is regenerable, so none of it
# belongs in git. The fact that a run happened does: ~300 bytes answering
# "when did this start failing" and "is the suite getting slower".
# ---------------------------------------------------------------------------

def test_an_unarmed_run_writes_no_ledger_row(tmp_path):
    """71 exploratory runs in a week would bury the runs somebody meant."""
    sink = RunLedgerSink(tmp_path / 'history')
    sink.finish([_r('t::a', 'passed')], _ctx())
    assert not (tmp_path / 'history').exists()
    assert sink.summary() == []


def test_concurrent_armed_runs_lose_no_ledger_row(tmp_path):
    """Two armed runs sharing a clone (a nightly overlapping a manual run on
    the box) both append to the same month file. Windows open(path, "a") is
    not atomic across handles, so this measured 38 of 40 rows before the
    lock. Every row must survive now, with no corruption."""
    d = tmp_path / 'history'

    def a_run(i):
        ctx = _ctx(run_key=f'r{i}', project_key='JlW1')
        RunLedgerSink(d).finish([_r(f't::{i}', 'passed')], ctx)

    threads = [threading.Thread(target=a_run, args=(i,)) for i in range(40)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    lines = (d / '2026-08.jsonl').read_text(encoding='utf-8').splitlines()
    keys = {json.loads(ln)['run_key'] for ln in lines if ln.strip()}
    assert len(lines) == 40                       # nothing lost
    assert keys == {f'r{i}' for i in range(40)}   # every run, no dup
    assert not list(d.glob('*.lock'))             # lock cleaned up


def test_concurrent_failure_log_appends_lose_nothing(tmp_path):
    """A retry CLI writing the failure log while a session appends to it used
    to clobber the session's new entry (unlocked read-modify-write). The
    locked append serializes them, so every entry survives."""
    from soterix_qa_shared.qatouch.results import locked_json_append
    log = tmp_path / 'upload_failures.json'

    def add(i):
        locked_json_append(log, {'run_key': f'r{i}', 'results': [{'case': f'TR{i}'}]})

    threads = [threading.Thread(target=add, args=(i,)) for i in range(40)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    got = json.loads(log.read_text(encoding='utf-8'))
    assert len(got) == 40
    assert len({e['run_key'] for e in got}) == 40


def test_an_atomic_write_leaves_no_stub_and_no_temp(tmp_path):
    """A half-written failure log used to read as an empty queue and discard
    every saved retry. The atomic write replaces the file whole, so a reader
    sees all-old or all-new, and no .tmp is left behind."""
    from soterix_qa_shared.qatouch.results import write_json_atomic
    log = tmp_path / 'q.json'
    write_json_atomic(log, [{'a': 1}])
    write_json_atomic(log, [{'a': 1}, {'b': 2}])
    assert json.loads(log.read_text(encoding='utf-8')) == [{'a': 1}, {'b': 2}]
    assert not list(tmp_path.glob('*.tmp'))


def test_a_corrupt_failure_log_is_not_appended_onto_blindly(tmp_path):
    """A truncated log reads as empty rather than raising, and the append
    starts a fresh list - it does not crash the save path."""
    from soterix_qa_shared.qatouch.results import locked_json_append
    log = tmp_path / 'q.json'
    log.write_text('[{"partial": ', encoding='utf-8')      # truncated
    locked_json_append(log, {'run_key': 'r1'})
    got = json.loads(log.read_text(encoding='utf-8'))
    assert got == [{'run_key': 'r1'}]


def test_a_stale_lock_is_broken_so_a_dead_holder_cannot_wedge_it(tmp_path):
    """A run that died holding the lock leaves the .lock file behind. A later
    run must not wait forever: a lock older than the timeout is stale and
    gets broken."""
    from soterix_qa_shared.qatouch.results import _locked_append
    target = tmp_path / '2026-08.jsonl'
    target.write_text('', encoding='utf-8')
    stale = tmp_path / '2026-08.jsonl.lock'
    stale.write_text('', encoding='utf-8')
    old = time.time() - 3600
    os.utime(stale, (old, old))                   # an hour-old lock
    _locked_append(target, 'row\n', timeout=0.5)
    assert target.read_text(encoding='utf-8') == 'row\n'
    assert not stale.exists()


def test_the_ledger_is_written_even_when_the_upload_fails(tmp_path):
    """The ledger records that a RUN happened, not that an upload did.

    QA Touch being down is exactly when the timeline matters most, and a
    ledger that skipped those runs would leave its gap over the bad week.
    The two sinks are independent by construction: the ledger never sees the
    upload's outcome. This pins that, because the docstring used to read "a
    run that uploaded to QA Touch", which invites someone to make the row
    conditional on a success it was never meant to depend on.
    """
    ledger = RunLedgerSink(tmp_path / 'history')
    results = [_r('t::a', 'passed')]
    context = _ctx(run_key='d6pw', project_key='JlW1')   # a run key IS armed

    class FailingUpload:
        name = 'qa touch'

        def finish(self, results, context):
            raise RuntimeError('QA Touch returned 503')

    try:
        FailingUpload().finish(results, context)
    except RuntimeError:
        pass
    ledger.finish(results, context)

    lines = (tmp_path / 'history' / '2026-08.jsonl').read_text(
        encoding='utf-8').splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])['counts'] == {'passed': 1, 'failed': 0, 'skipped': 0}
    assert ledger.written is True


def test_the_ledger_row_records_the_box_when_set(tmp_path):
    """One env can hold several tenants (India and US both on ENV=qa), so the
    row carries the box too - otherwise a reader cannot tell which tenant a
    run hit."""
    sink = RunLedgerSink(tmp_path / 'history')
    sink.finish([_r('t::a', 'passed')],
                _ctx(run_key='d6pw', env='qa', box='india', project_key='JlW1'))
    row = json.loads((tmp_path / 'history' / '2026-08.jsonl').read_text())
    assert row['env'] == 'qa'
    assert row['box'] == 'india'


def test_the_ledger_row_omits_box_when_unset_so_old_rows_still_compare(tmp_path):
    """A repo that sets no BOX writes no box key, so a single-tenant repo's
    rows stay byte-for-byte what they were."""
    sink = RunLedgerSink(tmp_path / 'history')
    sink.finish([_r('t::a', 'passed')], _ctx(run_key='d6pw', env='qa'))
    row = json.loads((tmp_path / 'history' / '2026-08.jsonl').read_text())
    assert 'box' not in row


def test_an_armed_run_appends_one_line(tmp_path):
    sink = RunLedgerSink(tmp_path / 'history')
    sink.finish([_r('t::a', 'passed'), _r('t::b', 'failed')],
                _ctx(run_key='d6pw', env='qa', branch='main', commit='0f912d0',
                     target=('tests/ui',), exitstatus=1))
    path = tmp_path / 'history' / '2026-08.jsonl'
    lines = path.read_text(encoding='utf-8').splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row['at'].startswith('2026-08-28T14:02')
    assert row['env'] == 'qa'
    assert row['branch'] == 'main'
    assert row['commit'] == '0f912d0'
    assert row['run_key'] == 'd6pw'
    assert row['target'] == ['tests/ui']
    assert row['dur_s'] == 1840.0
    assert row['exit'] == 1
    # The three standard outcomes are always present, even at zero, so
    # `.counts.failed` never reads null on a green run and two rows
    # can be compared key by key.
    assert row['counts'] == {'passed': 1, 'failed': 1, 'skipped': 0}
    assert row['failed'] == ['t::b']


def test_a_green_run_still_reports_a_failed_count_of_zero(tmp_path):
    """Built from what occurred, `counts` omitted 'failed' on an all-pass
    run - so `.counts.failed` read null for exactly the runs a reader most
    wants to filter on, and the documented recipe printed "null failed"."""
    sink = RunLedgerSink(tmp_path / 'history')
    sink.finish([_r(f't::{i}', 'passed') for i in range(604)], _ctx(run_key='d6pw'))
    row = json.loads((tmp_path / 'history' / '2026-08.jsonl').read_text())
    assert row['counts'] == {'passed': 604, 'failed': 0, 'skipped': 0}


def test_a_ledger_row_is_small(tmp_path):
    """The whole case for tracking this rests on the row staying tiny."""
    sink = RunLedgerSink(tmp_path / 'history')
    results = [_r(f'tests/ui/mod{i}.py::test_thing_number_{i}[chromium]', 'passed')
               for i in range(600)]
    results.append(_r('tests/ui/mod0.py::test_broken[chromium]', 'failed'))
    sink.finish(results, _ctx(run_key='d6pw', branch='main', commit='0f912d0'))
    size = (tmp_path / 'history' / '2026-08.jsonl').stat().st_size
    assert size < 1024, f'a 601-test run wrote {size} bytes'


def test_a_catastrophic_run_does_not_write_an_enormous_line(tmp_path):
    """The environment is down and 600 tests fail. The count must stay
    exact, but the list of names cannot be allowed to drown the file."""
    sink = RunLedgerSink(tmp_path / 'history', max_failures=25)
    results = [_r(f'tests/ui/mod{i}.py::test_{i}', 'failed') for i in range(600)]
    sink.finish(results, _ctx(run_key='d6pw'))
    row = json.loads((tmp_path / 'history' / '2026-08.jsonl').read_text())
    assert row['counts']['failed'] == 600      # exact
    assert len(row['failed']) == 25            # capped
    assert row['failed_truncated'] == 575      # and it says so


def test_a_run_with_no_truncation_says_nothing_about_it(tmp_path):
    sink = RunLedgerSink(tmp_path / 'history')
    sink.finish([_r('t::a', 'failed')], _ctx(run_key='d6pw'))
    row = json.loads((tmp_path / 'history' / '2026-08.jsonl').read_text())
    assert 'failed_truncated' not in row


def test_the_ledger_appends_rather_than_overwriting(tmp_path):
    """Append-only is the whole contract: a row that changes cannot be
    trusted, and history that overwrites is not history."""
    sink = RunLedgerSink(tmp_path / 'history')
    sink.finish([_r('t::a', 'passed')], _ctx(run_key='r1'))
    sink.finish([_r('t::a', 'failed')], _ctx(run_key='r2'))
    lines = (tmp_path / 'history' / '2026-08.jsonl').read_text().splitlines()
    assert [json.loads(x)['run_key'] for x in lines] == ['r1', 'r2']


def test_runs_in_different_months_land_in_different_files(tmp_path):
    """One file per month keeps concurrent appends from colliding as often,
    and keeps any one file readable."""
    sink = RunLedgerSink(tmp_path / 'history')
    one = [_r('t::a', 'passed')]
    sink.finish(one, _ctx(run_key='r1', started=datetime(2026, 8, 31, 23, 0, tzinfo=UTC)))
    sink.finish(one, _ctx(run_key='r2', started=datetime(2026, 9, 1, 1, 0, tzinfo=UTC)))
    names = sorted(p.name for p in (tmp_path / 'history').iterdir())
    assert names == ['2026-08.jsonl', '2026-09.jsonl']


def test_the_ledger_row_is_one_line_of_valid_json(tmp_path):
    """A node id with a quote or a backslash must not break the file for
    every reader that comes after it."""
    sink = RunLedgerSink(tmp_path / 'history')
    sink.finish([_r('tests/x.py::test_a["quoted"]', 'failed'),
                 _r('tests' + chr(92) + 'win.py::test_b', 'failed')],
                _ctx(run_key='d6pw'))
    text = (tmp_path / 'history' / '2026-08.jsonl').read_text(encoding='utf-8')
    assert len(text.strip().splitlines()) == 1
    assert len(json.loads(text)['failed']) == 2


def test_failures_are_sorted_so_two_runs_diff_cleanly(tmp_path):
    """Collection order shifts between runs; a stable order means a diff
    shows what actually changed."""
    sink = RunLedgerSink(tmp_path / 'history')
    sink.finish([_r('t::c', 'failed'), _r('t::a', 'failed'), _r('t::b', 'failed')],
                _ctx(run_key='d6pw'))
    row = json.loads((tmp_path / 'history' / '2026-08.jsonl').read_text())
    assert row['failed'] == ['t::a', 't::b', 't::c']


def test_an_unwritable_history_dir_never_raises(tmp_path):
    sink = RunLedgerSink(tmp_path / ('hist' + chr(0) + 'bad'))
    sink.finish([_r('t::a', 'passed')], _ctx(run_key='d6pw'))   # no raise
    assert sink.error is not None
    assert 'NOT written' in sink.summary()[0]


def test_duration_is_wall_clock_not_the_sum_of_test_times(tmp_path):
    """Collection, fixtures and teardown are the difference, and on a
    Playwright suite they are often most of it."""
    ctx = _ctx(run_key='d6pw')
    sink = RunLedgerSink(tmp_path / 'history')
    sink.finish([_r('t::a', 'passed', duration=0.1)], ctx)
    row = json.loads((tmp_path / 'history' / '2026-08.jsonl').read_text())
    assert row['dur_s'] == 1840.0


def test_armed_is_decided_by_the_run_key(tmp_path):
    assert _ctx(run_key='d6pw').armed is True
    assert _ctx().armed is False
