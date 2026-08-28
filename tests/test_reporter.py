"""The pytest plugin: paths, mapping policy, node-id records, sink fan-out.

Everything about talking to QA Touch lives in tests/test_upload_sink.py;
everything about the result shape lives in tests/test_results.py. What is
left here is the plugin itself.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from soterix_qa_shared.qatouch import reporter as qr
from soterix_qa_shared.qatouch.results import ResultSink, RunResultsFileSink
from soterix_qa_shared.qatouch.upload import QATouchUploadSink


def _permissive_policy(_path):
    """Stand-in for a consumer's injected mapping policy."""
    return {}, {'total': 0, 'reviewed': 0, 'unreviewed': 0}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """Every test runs against a tmp root with a policy injected."""
    qr.configure(tmp_path)
    monkeypatch.setattr(qr, 'MAPPING_POLICY', _permissive_policy)
    for var in ('QATOUCH_PROJECT_ID', 'QATOUCH_UPLOAD', 'QATOUCH_RUN_KEY',
                'QATOUCH_USE_PLAYWRIGHT_ENDPOINT'):
        monkeypatch.delenv(var, raising=False)
    yield


def _mapping(monkeypatch, tmp_path, mapping, counts=None):
    (tmp_path / 'config').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'config' / 'qatouch_case_mapping.json').write_text('{}', encoding='utf-8')
    monkeypatch.setattr(qr, 'MAPPING_POLICY',
                        lambda _p: (mapping, counts or {'total': len(mapping),
                                                        'unreviewed': 0}))


class _Recorder(ResultSink):
    """Captures what the reporter hands over."""

    name = 'recorder'

    def __init__(self):
        self.recorded = []
        self.finished = None

    def record(self, result):
        self.recorded.append(result)

    def finish(self, results):
        self.finished = list(results)


def _reporter(sinks=None):
    return qr.QATouchReporter(sinks if sinks is not None else [])


def _item(case_key=None, project_key=None):
    """Minimal stand-in for a pytest Item exposing get_closest_marker."""
    item = MagicMock()

    def marker(name):
        if name == 'qatouch' and case_key is not None:
            m = MagicMock()
            m.args = (case_key,)
            return m
        if name == 'qatouch_project' and project_key is not None:
            m = MagicMock()
            m.args = (project_key,)
            return m
        return None

    item.get_closest_marker.side_effect = marker
    item._qatouch_screenshot = None
    return item


def _config(tmp_path, options=None):
    config = MagicMock()
    config.rootpath = tmp_path
    opts = options or {}
    config.getoption.side_effect = lambda name, default=None: opts.get(name, default)
    return config


# ---------------------------------------------------------------------------
# configure() - the path seam extraction introduced
# ---------------------------------------------------------------------------

def test_configure_derives_every_writer_path_from_the_given_root(tmp_path):
    root = tmp_path / 'somerepo'
    qr.configure(root)
    assert qr.ROOT == root
    assert qr.MAPPING_PATH == root / 'config' / 'qatouch_case_mapping.json'
    assert qr.FAILURE_LOG_DIR == root / 'logs'
    assert qr.FAILURE_LOG_PATH == root / 'logs' / 'upload_failures.json'
    assert qr.RUN_RESULTS_PATH == root / 'logs' / 'last_run_results.json'


def test_run_record_sits_beside_the_failure_log(tmp_path):
    qr.configure(tmp_path)
    assert qr.RUN_RESULTS_PATH.parent == qr.FAILURE_LOG_DIR
    assert qr.RUN_RESULTS_PATH.name == 'last_run_results.json'
    assert qr.RUN_RESULTS_PATH.parent.name == 'logs'


def test_configure_accepts_a_string_root(tmp_path):
    """A CLI that resolved its root as a string must not have to convert it."""
    qr.configure(str(tmp_path))
    assert isinstance(qr.ROOT, Path)
    assert qr.FAILURE_LOG_DIR == tmp_path / 'logs'


def test_every_writer_path_stays_inside_the_given_root(tmp_path):
    """A path that escaped the root would write into someone else's repo."""
    qr.configure(tmp_path)
    for path in (qr.MAPPING_PATH, qr.FAILURE_LOG_DIR,
                 qr.FAILURE_LOG_PATH, qr.RUN_RESULTS_PATH):
        assert tmp_path in path.parents, path


# ---------------------------------------------------------------------------
# pytest_configure and the sink set
# ---------------------------------------------------------------------------

def test_an_unarmed_run_still_records_locally(tmp_path):
    """The gap the sink split closes. Before it, no upload meant no plugin,
    so the flaky history and the run cross-check saw nothing."""
    config = _config(tmp_path)
    qr.pytest_configure(config)
    config.pluginmanager.register.assert_called_once()
    reporter = config.pluginmanager.register.call_args[0][0]
    assert [type(s) for s in reporter.sinks] == [RunResultsFileSink]
    assert reporter.sinks[0].run_key is None


def test_an_armed_run_records_locally_and_uploads(tmp_path):
    config = _config(tmp_path, {'--qatouch-upload': True, '--qatouch-run': 'RUN1'})
    qr.pytest_configure(config)
    reporter = config.pluginmanager.register.call_args[0][0]
    assert [type(s) for s in reporter.sinks] == [RunResultsFileSink, QATouchUploadSink]
    assert reporter.sinks[0].run_key == 'RUN1'
    assert reporter.sinks[1].run_key == 'RUN1'


def test_arming_without_a_run_key_uploads_nothing_but_still_records(tmp_path):
    """The upload needs a run key; the local record never did."""
    config = _config(tmp_path, {'--qatouch-upload': True})
    qr.pytest_configure(config)
    reporter = config.pluginmanager.register.call_args[0][0]
    assert [type(s) for s in reporter.sinks] == [RunResultsFileSink]
    assert reporter.sinks[0].run_key is None


def test_the_run_record_can_be_switched_off(tmp_path):
    """With nothing left to do, the plugin does not register at all."""
    config = _config(tmp_path, {'--no-run-record': True})
    qr.pytest_configure(config)
    config.pluginmanager.register.assert_not_called()


def test_env_vars_arm_the_upload_like_the_flags_do(tmp_path, monkeypatch):
    monkeypatch.setenv('QATOUCH_UPLOAD', '1')
    monkeypatch.setenv('QATOUCH_RUN_KEY', 'ENVRUN')
    config = _config(tmp_path)
    qr.pytest_configure(config)
    reporter = config.pluginmanager.register.call_args[0][0]
    assert isinstance(reporter.sinks[-1], QATouchUploadSink)
    assert reporter.sinks[-1].run_key == 'ENVRUN'


def test_the_upload_sink_is_handed_the_configured_failure_log(tmp_path):
    """The package holds no path; the sink is given the consumer's."""
    config = _config(tmp_path, {'--qatouch-upload': True, '--qatouch-run': 'RUN1'})
    qr.pytest_configure(config)
    reporter = config.pluginmanager.register.call_args[0][0]
    assert reporter.sinks[-1].failure_log_path == tmp_path / 'logs' / 'upload_failures.json'


def test_pytest_configure_survives_an_unresolvable_root(monkeypatch, capsys):
    """An exception here would abort the whole session with INTERNALERROR."""
    monkeypatch.setattr(qr, 'configure', MagicMock(side_effect=OSError('bad rootdir')))
    config = MagicMock()
    qr.pytest_configure(config)   # must not raise
    assert 'could not resolve the repo root' in capsys.readouterr().out
    config.pluginmanager.register.assert_not_called()


def test_pytest_configure_survives_a_reporter_that_cannot_start(monkeypatch, capsys, tmp_path):
    """A constructor blow-up degrades to no reporting, never INTERNALERROR."""
    monkeypatch.setattr(qr, 'QATouchReporter', MagicMock(side_effect=RuntimeError('boom')))
    config = _config(tmp_path)
    qr.pytest_configure(config)   # must not raise
    assert 'the reporter could not start' in capsys.readouterr().out
    config.pluginmanager.register.assert_not_called()


def test_screenshot_capture_follows_what_the_sinks_ask_for(tmp_path, monkeypatch):
    """Capturing a PNG costs a browser round trip; only do it if a sink wants it."""
    assert _reporter([RunResultsFileSink(tmp_path / 'x.json')]).capture_screenshots is False
    monkeypatch.setenv('QATOUCH_USE_PLAYWRIGHT_ENDPOINT', '1')
    config = _config(tmp_path, {'--qatouch-upload': True, '--qatouch-run': 'RUN1'})
    qr.pytest_configure(config)
    assert config.pluginmanager.register.call_args[0][0].capture_screenshots is True


# ---------------------------------------------------------------------------
# The injected mapping policy
# ---------------------------------------------------------------------------

def test_no_policy_means_nothing_resolves_and_says_so(monkeypatch, capsys):
    """A broken shim must not fall back to a permissive default."""
    monkeypatch.setattr(qr, 'MAPPING_POLICY', None)
    assert _reporter().mapping == {}
    assert 'no mapping policy' in capsys.readouterr().out


def test_the_missing_policy_message_names_what_this_session_loses(monkeypatch, capsys, tmp_path):
    """Same fault, different cost. An unarmed run loses case attribution in
    a local file; an armed one uploads nothing. Saying "nothing will be
    uploaded" to someone who was not uploading trains people to ignore it."""
    monkeypatch.setattr(qr, 'MAPPING_POLICY', None)

    _reporter([RunResultsFileSink(tmp_path / 'x.json')])
    assert 'run record will carry no case keys' in capsys.readouterr().out

    _reporter([QATouchUploadSink(run_key='RUN1')])
    assert 'Nothing will be uploaded' in capsys.readouterr().out


def test_a_raising_policy_means_nothing_resolves_and_says_so(monkeypatch, capsys, tmp_path):
    """A policy that throws is reported, not swallowed into a silent zero."""
    (tmp_path / 'config').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'config' / 'qatouch_case_mapping.json').write_text('{}', encoding='utf-8')
    monkeypatch.setattr(qr, 'MAPPING_POLICY', MagicMock(side_effect=ValueError('malformed')))
    assert _reporter().mapping == {}
    assert 'could not load the mapping policy' in capsys.readouterr().out


def test_the_policy_receives_the_configured_mapping_path(monkeypatch, tmp_path):
    """The package holds no path of its own - the policy is handed the root's."""
    (tmp_path / 'config').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'config' / 'qatouch_case_mapping.json').write_text('{}', encoding='utf-8')
    policy = MagicMock(return_value=({}, {'total': 0, 'unreviewed': 0}))
    monkeypatch.setattr(qr, 'MAPPING_POLICY', policy)
    _reporter()
    policy.assert_called_once_with(tmp_path / 'config' / 'qatouch_case_mapping.json')


def test_a_policy_may_omit_the_unreviewed_count(monkeypatch, tmp_path):
    """The API repo's rows carry no `reviewed` key at all."""
    _mapping(monkeypatch, tmp_path, {'a::b': 'K1'}, counts={'total': 1})
    assert _reporter().mapping == {'a::b': 'K1'}


# ---------------------------------------------------------------------------
# Resolving a node id to a case
# ---------------------------------------------------------------------------

def test_a_marker_outranks_the_mapping(monkeypatch, tmp_path):
    _mapping(monkeypatch, tmp_path, {'t.py::test_a': 'FROM_MAP'})
    r = _reporter()
    assert r._resolve_case_key(_item(case_key='FROM_MARKER'), 't.py::test_a') == 'FROM_MARKER'


def test_an_exact_nodeid_resolves(monkeypatch, tmp_path):
    _mapping(monkeypatch, tmp_path, {'t.py::test_a[chromium]': 'K1'})
    assert _reporter()._resolve_case_key(None, 't.py::test_a[chromium]') == 'K1'


def test_a_stored_param_resolves_under_another_param(monkeypatch, tmp_path):
    """A [chromium] row must still resolve under [firefox] or no param.

    Measured before this: a firefox run matched 0 of 121 reviewed rows.
    """
    _mapping(monkeypatch, tmp_path, {'t.py::test_a[chromium]': 'K1'})
    r = _reporter()
    for nodeid in ('t.py::test_a[chromium]', 't.py::test_a[firefox]', 't.py::test_a'):
        assert r._resolve_case_key(None, nodeid) == 'K1', nodeid


def test_a_disputed_base_is_left_unresolved(monkeypatch, tmp_path):
    """Two rows disagreeing on the case key must not be guessed at -
    writing a status onto the wrong case is worse than writing none."""
    _mapping(monkeypatch, tmp_path,
             {'t.py::test_a[chromium]': 'K1', 't.py::test_a[firefox]': 'K2'})
    r = _reporter()
    assert r._resolve_case_key(None, 't.py::test_a[webkit]') is None
    # The exact matches still work; only the ambiguous collapse is refused.
    assert r._resolve_case_key(None, 't.py::test_a[chromium]') == 'K1'


def test_project_resolution_order(monkeypatch, tmp_path):
    """Marker, then QATOUCH_PROJECT_ID, then the default."""
    assert _reporter()._resolve_project_key(_item(project_key='PROJ_B')) == 'PROJ_B'
    assert _reporter()._resolve_project_key(_item()) == qr.DEFAULT_PROJECT_KEY
    monkeypatch.setenv('QATOUCH_PROJECT_ID', 'ENV_PROJ')
    assert _reporter()._resolve_project_key(_item()) == 'ENV_PROJ'
    assert _reporter()._resolve_project_key(None) == 'ENV_PROJ'


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

def test_every_test_is_recorded_mapped_or_not(monkeypatch, tmp_path):
    """An unmapped failure still deserves a bug, and the node-id record is
    what the bug filer reads."""
    _mapping(monkeypatch, tmp_path, {'t::mapped': 'K1'})
    r = _reporter()
    r.record(None, 't::mapped', 'passed', duration=0.1)
    r.record(None, 't::unmapped', 'failed', duration=0.2)
    assert set(r.records) == {'t::mapped', 't::unmapped'}
    assert r.records['t::unmapped'].case_key is None
    assert r.stats == {'mapped': 1, 'unmapped': 1}


def test_a_rerun_keeps_the_worse_outcome_and_its_evidence(monkeypatch, tmp_path):
    _mapping(monkeypatch, tmp_path, {'t::a': 'K1'})
    r = _reporter()
    r.record(None, 't::a', 'failed', duration=9.9)
    r.record(None, 't::a', 'passed', duration=0.1)
    assert len(r.records) == 1
    assert r.records['t::a'].outcome == 'failed'
    assert r.records['t::a'].duration == 9.9


def test_a_rerun_does_not_double_count_the_stats(monkeypatch, tmp_path):
    _mapping(monkeypatch, tmp_path, {'t::a': 'K1'})
    r = _reporter()
    r.record(None, 't::a', 'failed')
    r.record(None, 't::a', 'passed')
    assert r.stats == {'mapped': 1, 'unmapped': 0}


def test_two_tests_on_one_case_are_both_recorded(monkeypatch, tmp_path):
    """Keyed by case key, the second used to overwrite the first. 42 case
    keys in the UI mapping are claimed by more than one node id."""
    _mapping(monkeypatch, tmp_path, {'t::a': 'K1', 't::b': 'K1'})
    r = _reporter()
    r.record(None, 't::a', 'passed')
    r.record(None, 't::b', 'failed')
    assert len(r.records) == 2
    assert {rec.case_key for rec in r.records.values()} == {'K1'}


def test_a_screenshot_on_the_item_travels_with_the_record(monkeypatch, tmp_path):
    _mapping(monkeypatch, tmp_path, {'t::a': 'K1'})
    item = _item(case_key='K1')
    item._qatouch_screenshot = b'\x89PNG'
    r = _reporter()
    r.record(item, 't::a', 'failed')
    assert r.records['t::a'].screenshot == b'\x89PNG'


# ---------------------------------------------------------------------------
# Sink fan-out
# ---------------------------------------------------------------------------

def test_sinks_see_every_result_at_session_end():
    sink = _Recorder()
    r = _reporter([sink])
    r.record(None, 't::a', 'passed')
    r.record(None, 't::b', 'failed')
    r.pytest_sessionfinish(session=None, exitstatus=1)
    assert {x.nodeid for x in sink.finished} == {'t::a', 't::b'}


def test_sinks_see_results_as_they_happen_too():
    sink = _Recorder()
    r = _reporter([sink])
    r.record(None, 't::a', 'passed')
    assert [x.nodeid for x in sink.recorded] == ['t::a']


class _Broken(ResultSink):
    name = 'broken'

    def record(self, result):
        raise RuntimeError('record blew up')

    def finish(self, results):
        raise RuntimeError('finish blew up')


def test_one_broken_sink_does_not_cost_the_others(caplog):
    """A destination that fails must not take the run's other destinations
    with it, and must not fail the test session."""
    good = _Recorder()
    r = _reporter([_Broken(), good])
    r.record(None, 't::a', 'passed')
    r.pytest_sessionfinish(session=None, exitstatus=0)
    assert [x.nodeid for x in good.finished] == ['t::a']
    assert 'broken' in caplog.text


def test_the_terminal_summary_asks_each_sink_for_its_lines():
    class _Chatty(ResultSink):
        name = 'chatty'

        def finish(self, results):
            pass

        def summary(self):
            return ['  Chatty:     said something']

    r = _reporter([_Chatty()])
    r.record(None, 't::a', 'passed')
    tr = MagicMock()
    r.pytest_terminal_summary(tr, exitstatus=0)
    written = ' '.join(str(c.args[0]) for c in tr.write_line.call_args_list)
    assert 'Chatty' in written
    assert 'Unmapped:   1 test (no case mapping)' in written


def test_the_summary_separates_the_test_count_from_the_case_count(monkeypatch, tmp_path):
    """Two tests on one case is one uploaded status covering both. Reporting
    only "2 results" hides that a single status stands for two tests."""
    _mapping(monkeypatch, tmp_path, {'t::a': 'K1', 't::b': 'K1', 't::c': 'K2'})
    r = _reporter()
    for nodeid in ('t::a', 't::b', 't::c'):
        r.record(None, nodeid, 'passed')
    tr = MagicMock()
    r.pytest_terminal_summary(tr, exitstatus=0)
    written = ' '.join(str(c.args[0]) for c in tr.write_line.call_args_list)
    assert 'Mapped:     3 tests -> 2 cases' in written
