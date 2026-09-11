"""Pins for the live record's wiring in run_archive: the hooks write it at
collection, refresh it as tests finish, throttle the refreshes, and take it
down when the session ends, whatever the archive then decides."""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from soterix_qa_shared.reporting import live
from soterix_qa_shared.reporting import run_archive as ra


class FakeConfig:
    def __init__(self, args, options=None):
        self.args = list(args)
        self.rootpath = None
        self._options = options or {}

    def getoption(self, name, default=None):
        return self._options.get(name, default)


def fake_session(items, options=None):
    return SimpleNamespace(config=FakeConfig(['tests/ui'], options),
                           items=[SimpleNamespace(nodeid=n) for n in items])


def fake_report(nodeid, when='call', outcome='passed'):
    return SimpleNamespace(nodeid=nodeid, when=when, outcome=outcome, duration=1.0,
                           longrepr=None, sections=[], keywords={}, capstdout='', capstderr='')


@pytest.fixture
def state(monkeypatch, tmp_path):
    """A fresh run state rooted at tmp_path, with the live record enabled."""
    runs = tmp_path / 'runs'
    runs.mkdir()
    monkeypatch.setattr(ra, 'RUNS_DIR', runs)
    monkeypatch.setattr(ra, 'RUN_LOG', tmp_path / 'run.log')
    fresh = ra._RunState()
    fresh.started = time.time() - 30
    fresh.mapping = {}
    fresh.mapping_project, fresh.mapping_project_read = 'JlW1', True
    monkeypatch.setattr(ra, '_state', fresh)
    monkeypatch.setattr(ra, '_live_enabled', lambda config: True)
    monkeypatch.setattr(ra, '_archive_disabled', lambda environ: True)   # unconfigure stops after the clear
    monkeypatch.delenv('QA_RUNS_LIVE_DIR', raising=False)
    monkeypatch.setenv('BOX', 'us')
    return fresh


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def test_collection_finish_writes_the_first_record(state, tmp_path, monkeypatch):
    """The record exists from collection: what was collected, the workers, the box, the product."""
    served = tmp_path / 'served'
    monkeypatch.setenv('QA_RUNS_LIVE_DIR', str(served))
    ra.pytest_collection_finish(fake_session(['t/a.py::test_a', 't/b.py::test_b', 't/c.py::test_c'],
                                             {'numprocesses': 4}))
    record = read(ra.RUNS_DIR / 'live.json')
    assert record['state'] == 'running'
    assert (record['collected'], record['done'], record['workers'], record['box']) == (3, 0, 4, 'us')
    assert record['target'] == ['tests/ui']
    assert record['product'] == 'NexaiQ-Core'
    assert record['eta_basis'].startswith('no earlier run')
    assert read(served / 'live.json') == record        # the served copy is the same record


def test_progress_updates_the_record_and_names_the_current_test(state):
    """A finished test moves the counts and the remaining list; logstart names the test running now."""
    ra.pytest_collection_finish(fake_session(['t/a.py::test_a', 't/b.py::test_b']))
    ra.pytest_runtest_logstart('t/a.py::test_a', None)
    state.live_written = 0.0                              # past the throttle
    ra.pytest_runtest_logreport(fake_report('t/a.py::test_a'))
    record = read(ra.RUNS_DIR / 'live.json')
    assert record['done'] == 1
    assert record['counts']['passed'] == 1
    assert record['current'] == 't/a.py::test_a'
    assert record['elapsed_s'] >= 30


def test_refreshes_are_throttled(state):
    """Two finishes inside the gap cost one write."""
    ra.pytest_collection_finish(fake_session(['t/a.py::test_a', 't/b.py::test_b']))
    first = (ra.RUNS_DIR / 'live.json').read_text(encoding='utf-8')
    ra.pytest_runtest_logreport(fake_report('t/a.py::test_a'))    # inside MIN_WRITE_GAP_S of the first write
    assert (ra.RUNS_DIR / 'live.json').read_text(encoding='utf-8') == first
    assert state.counts['passed'] == 1                    # the count moved; only the file waited


def test_unconfigure_takes_the_record_down_before_any_early_return(state, tmp_path, monkeypatch):
    """The banner never outlives the run: the record goes even when the archive is skipped."""
    served = tmp_path / 'served'
    monkeypatch.setenv('QA_RUNS_LIVE_DIR', str(served))
    ra.pytest_collection_finish(fake_session(['t/a.py::test_a']))
    assert (ra.RUNS_DIR / 'live.json').exists() and (served / 'live.json').exists()
    ra.pytest_unconfigure(FakeConfig(['tests/ui']))
    assert not (ra.RUNS_DIR / 'live.json').exists()
    assert not (served / 'live.json').exists()


def test_disabled_live_record_writes_nothing(state, monkeypatch):
    """With the archive off (or in a worker process) no record is written and the hooks stay quiet."""
    monkeypatch.setattr(ra, '_live_enabled', lambda config: False)
    ra.pytest_collection_finish(fake_session(['t/a.py::test_a']))
    ra.pytest_runtest_logreport(fake_report('t/a.py::test_a'))
    assert not (ra.RUNS_DIR / 'live.json').exists()


def test_both_pages_carry_the_banner_slot():
    """The archive index and the health page fetch live.json beside themselves."""
    from soterix_qa_shared.reporting import health
    index = ra._render_index([])
    page = health.render_health(health.aggregate([], 5), runs_dir=None)
    for html in (index, page):
        assert live.banner_html() in html
        assert html.index('<h1>') < html.index('id="live"')


def test_sessionstart_opens_the_record_before_collection(state, monkeypatch):
    """The record exists from the first second, saying 'collecting' until the ids are known.
    This is the path the xdist controller takes: it never collects, so collection_finish
    never fires there (09/11/2026: every -n run, the nightly included, had no record at all)."""
    monkeypatch.setattr(ra, '_archive_disabled', lambda environ: False)
    monkeypatch.setattr(ra, '_process_may_archive', lambda config, environ: True)
    monkeypatch.setattr(ra, 'configure', lambda root: None)
    try:
        ra.pytest_sessionstart(SimpleNamespace(config=FakeConfig(['tests/ui'], {'numprocesses': 4})))
    finally:
        ra._state.run_log.detach()
    record = read(ra.RUNS_DIR / 'live.json')
    assert (record['collected'], record['done'], record['workers']) == (0, 0, 4)
    assert record['eta_s'] is None
    assert 'collecting' in record['eta_basis']


def test_xdist_controller_learns_the_ids_from_the_first_worker(state):
    """The first worker's collection names the run's tests; the other workers repeat the same ids."""
    ra._start_live(FakeConfig(['tests/ui'], {'numprocesses': 2}))
    assert read(ra.RUNS_DIR / 'live.json')['collected'] == 0
    ra.pytest_xdist_node_collection_finished(None, ['t/a.py::test_a', 't/b.py::test_b'])
    ra.pytest_xdist_node_collection_finished(None, ['t/a.py::test_a', 't/b.py::test_b'])
    record = read(ra.RUNS_DIR / 'live.json')
    assert record['collected'] == 2
    assert record['eta_basis'].startswith('no earlier run')
    ra.pytest_runtest_logstart('t/a.py::test_a', None)
    state.live_written = 0.0
    ra.pytest_runtest_logreport(fake_report('t/a.py::test_a'))
    assert read(ra.RUNS_DIR / 'live.json')['done'] == 1


def test_worker_ids_without_a_started_record_write_nothing(state):
    """A worker process (or a disabled record) has no live paths, so the ids hook stays quiet."""
    ra.pytest_xdist_node_collection_finished(None, ['t/a.py::test_a'])
    assert not (ra.RUNS_DIR / 'live.json').exists()


def test_banner_names_the_collecting_state():
    assert "'collecting the tests'" in live.banner_html()
