"""Pins for reporting/live.py: where the record goes, what it says, and how
the estimate reads the archive's own history."""
from __future__ import annotations

import json
from pathlib import Path

from soterix_qa_shared.reporting import live


def _summary(folder: Path, target, tests, duration_s, provenance=None):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'summary.json').write_text(json.dumps({
        'target': target, 'duration_s': duration_s, 'provenance': provenance or {'env': 'qa'},
        'tests': [{'nodeid': n, 'duration_s': d, 'outcome': 'passed'} for n, d in tests.items()],
    }), encoding='utf-8')


def test_live_paths_is_the_file_beside_the_index(tmp_path):
    """One path, beside the archive index. No environment variable adds another."""
    assert live.live_paths(tmp_path) == [tmp_path / 'live.json']


def test_write_and_clear_live_are_atomic_and_forgiving(tmp_path):
    """The record lands whole in every writable path, and clearing a missing file is not an error."""
    paths = live.live_paths(tmp_path) + [tmp_path / 'mirror' / 'live.json']
    live.write_live(paths, {'state': 'running', 'done': 3})
    for p in paths:
        assert json.loads(p.read_text(encoding='utf-8')) == {'state': 'running', 'done': 3}
        assert not p.with_suffix('.json.tmp').exists()
    live.clear_live(paths)
    assert not any(p.exists() for p in paths)
    live.clear_live(paths)  # already gone: still fine


def test_history_reads_only_the_same_target_newest_first(tmp_path):
    """Other targets and folders without a summary (a killed run) are not history. A run
    archived by an older version with a tenant label in its provenance is history like any other."""
    _summary(tmp_path / '2026-09-08_04-00-00__tests-ui', ['tests/ui'], {'a': 10}, 3600)
    _summary(tmp_path / '2026-09-09_04-00-00__tests-ui', ['tests/ui'], {'a': 12}, 3700)
    _summary(tmp_path / '2026-09-09_05-00-00__tests-ui', ['tests/ui'], {'a': 99}, 9000,
             provenance={'env': 'qa', 'box': 'old-label'})
    _summary(tmp_path / '2026-09-09_06-00-00__tests-ui-maps', ['tests/ui/maps'], {'a': 1}, 60)
    (tmp_path / '2026-09-10_04-00-00__tests-ui').mkdir()  # the cap killed it: no summary
    found = live.history(tmp_path, ['tests/ui'], last=5)
    assert [s['duration_s'] for s in found] == [9000, 3700, 3600]
    assert live.history(tmp_path, ['tests/ui'], last=1)[0]['duration_s'] == 9000
    assert live.history(tmp_path / 'nowhere', ['tests/ui']) == []


def test_median_durations_and_estimate_from_per_test_history():
    """Known tests cost their median; unknown ones the median of the known; divided by workers."""
    summaries = [{'tests': [{'nodeid': 'a', 'duration_s': 10}, {'nodeid': 'b', 'duration_s': 30}]},
                 {'tests': [{'nodeid': 'a', 'duration_s': 20}, {'nodeid': 'b', 'duration_s': 30}]},
                 {'tests': [{'nodeid': 'a', 'duration_s': 30}]}]
    durations = live.median_durations(summaries)
    assert durations == {'a': 20, 'b': 30}
    eta, basis = live.estimate_remaining_s(['a', 'b', 'new'], durations, workers=2, summaries=summaries)
    # a 20 + b 30 + new at the median of known (25) = 75 s of work over 2 workers
    assert eta == 37.5
    assert basis == 'per-test durations from 3 earlier runs of this target'


def test_estimate_falls_back_to_run_totals_then_to_none():
    """No per-test history: the median run length less elapsed, never negative. No runs: None with the reason."""
    runs = [{'duration_s': 3600}, {'duration_s': 4000}, {'duration_s': 3800}]
    assert live.estimate_remaining_s(['x'], {}, 4, runs, elapsed_s=600) == (
        3200, 'the median length of 3 earlier runs of this target')
    assert live.estimate_remaining_s(['x'], {}, 4, runs, elapsed_s=99999)[0] == 0
    eta, basis = live.estimate_remaining_s(['x'], {}, 4, [], elapsed_s=0)
    assert eta is None
    assert basis == 'no earlier run of this target, so no estimate'


def test_build_record_carries_what_the_banner_shows(tmp_path):
    """Progress, counts, the current test, elapsed and the estimate with its basis."""
    _summary(tmp_path / '2026-09-09_04-00-00__tests-ui', ['tests/ui'],
             {'t1': 10, 't2': 20, 't3': 30}, 3600)
    record = live.build_record(
        started=1000.0, now=1100.0, target=['tests/ui'], env='qa', product='NexaiQ-Core',
        workers=2, collected=3, counts={'passed': 1, 'failed': 0, 'skipped': 0, 'errors': 0},
        current='t2', remaining=['t2', 't3'], runs_dir=tmp_path, hostname='runner-1')
    assert record['state'] == 'running'
    assert (record['done'], record['collected'], record['current']) == (1, 3, 't2')
    assert record['elapsed_s'] == 100.0
    assert record['eta_s'] == 25.0            # (20 + 30) / 2 workers
    assert record['eta_basis'].startswith('per-test durations from 1 earlier run')
    assert record['host'] == 'runner-1' and 'box' not in record
    assert record['stale_after_s'] == live.STALE_AFTER_S


def test_banner_html_fetches_the_record_beside_the_page():
    """The pages stay static: a slot, a style, and a script that polls live.json next to the page."""
    html = live.banner_html()
    assert '<div id="live" class="live" hidden></div>' in html
    assert "fetch('live.json',{cache:'no-store'})" in html
    assert 'setInterval(poll,30000)' in html
    assert 'Run stalled' in html and 'Run in progress' in html
    assert 'http' not in html   # nothing loads from anywhere else
