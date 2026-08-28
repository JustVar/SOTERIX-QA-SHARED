"""The retry CLI.

The failure log exists because QA Touch answers success for uploads it
discards. Everything here defends the same property: an entry leaves the
log only when the run demonstrably carries the status that was sent.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

import soterix_qa_shared.qatouch as qatouch_pkg
from soterix_qa_shared.qatouch import retry


@pytest.fixture(autouse=True)
def _rooted(tmp_path):
    retry.configure(tmp_path)
    yield


def _write_log(tmp_path, entries, name='upload_failures.json'):
    (tmp_path / 'logs').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'logs' / name).write_text(json.dumps(entries), encoding='utf-8')


def _entry(results, run_key='RUN1', **kw):
    base = {'timestamp': 't', 'project_key': 'JlW1', 'run_key': run_key,
            'result_count': len(results), 'results': results,
            'comment': 'c', 'error': 'e'}
    base.update(kw)
    return base


def _fake_client(monkeypatch, **attrs):
    client = MagicMock(**attrs)
    monkeypatch.setattr(qatouch_pkg, 'QATouchClient', MagicMock(return_value=client))
    return client


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

def test_configure_puts_both_logs_under_the_repo_root(tmp_path):
    retry.configure(tmp_path)
    assert retry.FAILURE_LOG_PATH == tmp_path / 'logs' / 'upload_failures.json'
    assert retry.UNREPLAYABLE_PATH == tmp_path / 'logs' / 'upload_failures_unreplayable.json'


def test_the_retry_and_reporter_agree_on_the_failure_log(tmp_path):
    """Two modules deriving the same path separately is how a retry ends up
    reading a file nothing writes."""
    from soterix_qa_shared.qatouch import reporter
    reporter.configure(tmp_path)
    retry.configure(tmp_path)
    assert reporter.FAILURE_LOG_PATH == retry.FAILURE_LOG_PATH


def test_no_configured_root_says_so_rather_than_reporting_nothing_to_do(monkeypatch, capsys):
    """Exit 0 with "nothing to retry" would look like success."""
    monkeypatch.setattr(retry, 'FAILURE_LOG_PATH', None)
    assert retry.main([]) == 2
    assert 'No repo root was configured' in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Reading the log
# ---------------------------------------------------------------------------

def test_no_log_is_not_an_error(capsys):
    assert retry.main([]) == 0
    assert 'nothing to retry' in capsys.readouterr().out


def test_an_empty_log_is_not_an_error(tmp_path, capsys):
    _write_log(tmp_path, [])
    assert retry.main([]) == 0
    assert 'empty' in capsys.readouterr().out


def test_a_malformed_log_fails_rather_than_claiming_success(tmp_path, capsys):
    (tmp_path / 'logs').mkdir(parents=True)
    (tmp_path / 'logs' / 'upload_failures.json').write_text('not json', encoding='utf-8')
    assert retry.main([]) == 2
    assert 'malformed' in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Replayability
# ---------------------------------------------------------------------------

def test_an_entry_of_codes_is_replayable():
    assert retry._replayable(_entry([{'case': 'TR0262', 'status': 1}]))


def test_an_entry_holding_a_case_key_is_not_replayable():
    """The API accepts a case key, answers success, and writes nothing."""
    assert not retry._replayable(_entry([{'case': 'B869bm', 'status': 1}]))


def test_a_mixed_entry_is_not_replayable():
    assert not retry._replayable(_entry([{'case': 'TR0262', 'status': 1},
                                         {'case': 'B869bm', 'status': 5}]))


def test_translate_uses_the_runs_own_rows():
    entry = _entry([{'case': 'B869bm', 'status': 1}, {'case': 'TR0263', 'status': 5}])
    rows = [{'case_id': 'B869bm', 'code': 'TR0262'}]
    complete, out = retry.translate_keys_to_codes(entry, rows)
    assert complete is True
    assert out == [{'case': 'TR0262', 'status': 1}, {'case': 'TR0263', 'status': 5}]


def test_translate_reports_incomplete_when_a_key_is_not_in_the_run():
    entry = _entry([{'case': 'B869bm', 'status': 1}, {'case': 'MISSING', 'status': 5}])
    complete, out = retry.translate_keys_to_codes(entry, [{'case_id': 'B869bm', 'code': 'TR0262'}])
    assert complete is False
    assert out == [{'case': 'TR0262', 'status': 1}]


# ---------------------------------------------------------------------------
# --dry-run and --clear
# ---------------------------------------------------------------------------

def test_dry_run_touches_nothing(tmp_path, monkeypatch, capsys):
    client = _fake_client(monkeypatch)
    _write_log(tmp_path, [_entry([{'case': 'TR0262', 'status': 1}])])
    assert retry.main(['--dry-run']) == 0
    client.update_test_run_results_by_code.assert_not_called()
    assert (tmp_path / 'logs' / 'upload_failures.json').exists()
    assert 'Run without --dry-run' in capsys.readouterr().out


def test_dry_run_names_the_entries_that_cannot_be_replayed(tmp_path, capsys):
    _write_log(tmp_path, [_entry([{'case': 'B869bm', 'status': 1}])])
    retry.main(['--dry-run'])
    assert 'NOT replayable' in capsys.readouterr().out


def test_clear_empties_the_log_without_uploading(tmp_path, monkeypatch):
    client = _fake_client(monkeypatch)
    _write_log(tmp_path, [_entry([{'case': 'TR0262', 'status': 1}])])
    assert retry.main(['--clear']) == 0
    client.update_test_run_results_by_code.assert_not_called()
    assert not (tmp_path / 'logs' / 'upload_failures.json').exists()


# ---------------------------------------------------------------------------
# Replaying
# ---------------------------------------------------------------------------

def test_a_verified_replay_clears_the_log(tmp_path, monkeypatch):
    client = _fake_client(monkeypatch)
    client.verify_results_landed.return_value = {
        'checked': 1, 'landed': ['TR0262'], 'mismatched': [], 'missing': []}
    _write_log(tmp_path, [_entry([{'case': 'TR0262', 'status': 1}])])

    assert retry.main([]) == 0
    kwargs = client.update_test_run_results_by_code.call_args.kwargs
    assert kwargs['results'] == [{'case': 'TR0262', 'status': 1}]
    assert kwargs['comments'].endswith('(retry)')
    assert not (tmp_path / 'logs' / 'upload_failures.json').exists()


def test_a_replay_that_does_not_land_stays_in_the_log(tmp_path, monkeypatch):
    """The whole point: the API said success, the run disagrees."""
    client = _fake_client(monkeypatch)
    client.verify_results_landed.return_value = {
        'checked': 1, 'landed': [],
        'mismatched': ['TR0262: sent Passed, run shows Untested'], 'missing': []}
    _write_log(tmp_path, [_entry([{'case': 'TR0262', 'status': 1}])])

    assert retry.main([]) == 1
    kept = json.loads((tmp_path / 'logs' / 'upload_failures.json').read_text())
    assert len(kept) == 1


def test_a_raising_upload_keeps_the_entry(tmp_path, monkeypatch):
    client = _fake_client(monkeypatch)
    client.update_test_run_results_by_code.side_effect = RuntimeError('boom')
    _write_log(tmp_path, [_entry([{'case': 'TR0262', 'status': 1}])])

    assert retry.main([]) == 1
    assert len(json.loads((tmp_path / 'logs' / 'upload_failures.json').read_text())) == 1


def test_one_failing_entry_does_not_abandon_the_others(tmp_path, monkeypatch):
    client = _fake_client(monkeypatch)
    calls = []

    def update(**kw):
        calls.append(kw)
        if kw['test_run_key'] == 'BAD':
            raise RuntimeError('unreachable')

    client.update_test_run_results_by_code.side_effect = update
    client.verify_results_landed.return_value = {
        'checked': 1, 'landed': ['TR0263'], 'mismatched': [], 'missing': []}
    _write_log(tmp_path, [
        _entry([{'case': 'TR0262', 'status': 1}], run_key='BAD'),
        _entry([{'case': 'TR0263', 'status': 1}], run_key='GOOD'),
    ])

    assert retry.main([]) == 1
    assert [c['test_run_key'] for c in calls] == ['BAD', 'GOOD']
    kept = json.loads((tmp_path / 'logs' / 'upload_failures.json').read_text())
    assert [e['run_key'] for e in kept] == ['BAD']


# ---------------------------------------------------------------------------
# Key-based entries
# ---------------------------------------------------------------------------

def test_a_key_entry_is_resolved_from_the_run_and_replayed(tmp_path, monkeypatch):
    client = _fake_client(monkeypatch)
    client.get_all_test_run_results.return_value = [{'case_id': 'B869bm', 'code': 'TR0262'}]
    client.verify_results_landed.return_value = {
        'checked': 1, 'landed': ['TR0262'], 'mismatched': [], 'missing': []}
    _write_log(tmp_path, [_entry([{'case': 'B869bm', 'status': 5}])])

    assert retry.main([]) == 0
    kwargs = client.update_test_run_results_by_code.call_args.kwargs
    assert kwargs['results'] == [{'case': 'TR0262', 'status': 5}]


def test_an_unresolvable_key_entry_is_parked_not_replayed(tmp_path, monkeypatch, capsys):
    """Replaying it would send a case key: accepted, ignored, reported as
    success. The log would empty and nothing would have been written."""
    client = _fake_client(monkeypatch)
    client.get_all_test_run_results.return_value = []   # run has no matching row
    _write_log(tmp_path, [_entry([{'case': 'B869bm', 'status': 5}])])

    assert retry.main([]) == 1
    client.update_test_run_results_by_code.assert_not_called()
    assert not (tmp_path / 'logs' / 'upload_failures.json').exists()
    parked = json.loads(
        (tmp_path / 'logs' / 'upload_failures_unreplayable.json').read_text())
    assert parked[0]['results'][0]['case'] == 'B869bm'
    assert 'cannot be replayed' in capsys.readouterr().out


def test_a_partly_resolvable_entry_is_parked_whole_not_replayed_in_part(tmp_path, monkeypatch):
    """Replaying the resolved half would drop the rest with no trace.

    The entry then leaves the failure log having uploaded some of what it
    held, which reads as a clean retry. Either the whole entry replays or
    none of it does.
    """
    client = _fake_client(monkeypatch)
    client.get_all_test_run_results.return_value = [
        {'case_id': 'RESOLVES', 'code': 'TR0262'}]      # the other key is absent
    _write_log(tmp_path, [_entry([{'case': 'RESOLVES', 'status': 1},
                                  {'case': 'MISSING', 'status': 5}])])

    assert retry.main([]) == 1
    client.update_test_run_results_by_code.assert_not_called()
    parked = json.loads(
        (tmp_path / 'logs' / 'upload_failures_unreplayable.json').read_text())
    assert len(parked) == 1
    assert [r['case'] for r in parked[0]['results']] == ['RESOLVES', 'MISSING']


def test_parking_appends_rather_than_overwriting(tmp_path, monkeypatch):
    _fake_client(monkeypatch, get_all_test_run_results=MagicMock(return_value=[]))
    _write_log(tmp_path, [_entry([{'case': 'OLD1', 'status': 1}])],
               name='upload_failures_unreplayable.json')
    _write_log(tmp_path, [_entry([{'case': 'NEW1', 'status': 5}])])

    retry.main([])
    parked = json.loads(
        (tmp_path / 'logs' / 'upload_failures_unreplayable.json').read_text())
    assert [e['results'][0]['case'] for e in parked] == ['OLD1', 'NEW1']


def test_a_malformed_unreplayable_log_does_not_lose_the_new_entry(tmp_path, monkeypatch):
    _fake_client(monkeypatch, get_all_test_run_results=MagicMock(return_value=[]))
    (tmp_path / 'logs').mkdir(parents=True)
    (tmp_path / 'logs' / 'upload_failures_unreplayable.json').write_text(
        'not json', encoding='utf-8')
    _write_log(tmp_path, [_entry([{'case': 'B869bm', 'status': 5}])])

    retry.main([])
    parked = json.loads(
        (tmp_path / 'logs' / 'upload_failures_unreplayable.json').read_text())
    assert len(parked) == 1


def test_an_unreachable_client_leaves_key_entries_alone(tmp_path, monkeypatch, capsys):
    """A client that cannot start is not evidence the entries are junk."""
    monkeypatch.setattr(qatouch_pkg, 'QATouchClient',
                        MagicMock(side_effect=RuntimeError('no credentials')))
    _write_log(tmp_path, [_entry([{'case': 'B869bm', 'status': 5}])])

    assert retry.main([]) == 1
    parked = json.loads(
        (tmp_path / 'logs' / 'upload_failures_unreplayable.json').read_text())
    assert len(parked) == 1
    assert 'could not resolve key-based entries' in capsys.readouterr().out
