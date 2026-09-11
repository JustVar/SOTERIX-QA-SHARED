"""Media capture: the offline surface.

The full plugin needs Playwright and page objects, which the consumer repo
exercises through the shim. Here: the pure naming/config/off-value logic and
that configure() roots the evidence dirs where the caller says, since that
is the rule-5 change the extraction made.
"""
from __future__ import annotations

import os as _os
import time as _time
from types import SimpleNamespace as _NS

import pytest

from soterix_qa_shared.reporting import media


@pytest.fixture(autouse=True)
def _default_root(tmp_path):
    media.configure(tmp_path)
    yield


# ---------------------------------------------------------------------------
# configure: the rule-5 change
# ---------------------------------------------------------------------------

def test_configure_roots_the_evidence_dirs(tmp_path):
    media.configure(tmp_path)
    assert media.SCREENSHOT_DIR == tmp_path / 'reports' / 'screenshots'
    assert media.TRACE_DIR == tmp_path / 'reports' / 'artifacts'


def test_a_different_root_moves_both_dirs(tmp_path):
    other = tmp_path / 'elsewhere'
    media.configure(other)
    assert str(media.SCREENSHOT_DIR).startswith(str(other))
    assert str(media.TRACE_DIR).startswith(str(other))


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------

def test_safe_name_strips_a_node_id_to_a_filename(tmp_path):
    got = media._safe_name('tests/ui/views/test_views.py::TestViews::test_open[chromium]')
    assert '/' not in got
    assert '::' not in got
    assert '[' not in got


def test_final_and_capture_filenames_end_in_png(tmp_path):
    assert media._final_filename('t::a', 'PASSED').endswith('.png')
    assert media._capture_filename('t::a', 'FAILED', 'step', 3).endswith('.png')


def test_the_trace_path_is_under_the_configured_artifacts_dir(tmp_path):
    media.configure(tmp_path)
    p = media._expected_trace_path('tests/ui/test_x.py::test_y')
    assert p.suffix == '.zip'
    assert str(p).startswith(str(tmp_path / 'reports' / 'artifacts'))


# ---------------------------------------------------------------------------
# Off-value parsing (the flags a consumer sets in the environment)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('value', ['off', '0', 'false', 'no'])
def test_screenshots_can_be_switched_off(value):
    assert media._screenshots_disabled({'REPORT_SCREENSHOTS': value}) is True


def test_screenshots_are_on_by_default():
    assert media._screenshots_disabled({}) is False


def test_suffix_reflects_the_test_outcome():
    assert media._suffix_for('call', 'passed') == 'PASSED'
    assert media._suffix_for('call', 'failed') == 'FAILED'


# ---------------------------------------------------------------------------
# Cleaning the screenshot dir
# ---------------------------------------------------------------------------

def test_clean_removes_only_pngs_and_reports_the_count(tmp_path):
    d = tmp_path / 'shots'
    d.mkdir()
    (d / 'a.png').write_bytes(b'\x89PNG')
    (d / 'b.png').write_bytes(b'\x89PNG')
    (d / 'keep.txt').write_text('x', encoding='utf-8')
    removed = media._clean_screenshot_dir(d)
    assert removed == 2
    assert (d / 'keep.txt').exists()
    assert not list(d.glob('*.png'))


def test_clean_is_quiet_on_a_missing_dir(tmp_path):
    assert media._clean_screenshot_dir(tmp_path / 'nope') == 0


# ---------------------------------------------------------------------------
# The watchdog: a screenshot call that never returns must not hang a worker
# ---------------------------------------------------------------------------



def test_hang_cap_reads_the_env_and_defaults():
    assert media._hang_cap({}) == media.DEFAULT_HANG_CAP_S
    assert media._hang_cap({'REPORT_MEDIA_CAP_S': '12'}) == 12.0
    assert media._hang_cap({'REPORT_MEDIA_CAP_S': '0'}) == 0.0
    assert media._hang_cap({'REPORT_MEDIA_CAP_S': 'soon'}) == media.DEFAULT_HANG_CAP_S


def _page_with_driver(proc):
    return _NS(_impl_obj=_NS(_connection=_NS(_transport=_NS(_proc=proc))))


def test_driver_alive_reads_the_process_state():
    """A reaped driver is dead; a running one is alive where /proc exists; anything unreadable is unknown."""
    assert media._driver_alive(_page_with_driver(_NS(pid=4242, returncode=0))) is False
    assert media._driver_alive(_NS()) is None
    own = media._driver_alive(_page_with_driver(_NS(pid=_os.getpid(), returncode=None)))
    assert own is (True if _os.path.exists('/proc') else None)


def test_watchdog_ends_an_xdist_worker_and_only_says_so_elsewhere(monkeypatch):
    """Past the cap, a worker exits (the controller reschedules); a plain session is only told."""
    exits: list[int] = []
    monkeypatch.setattr(media, '_exit', lambda code: exits.append(code))
    with media._Watchdog('t.py::test_a', 0.05, {'PYTEST_XDIST_WORKER': 'gw1'}) as w:
        _time.sleep(0.3)
    assert w.expired and exits == [3]
    exits.clear()
    with media._Watchdog('t.py::test_a', 0.05, {}) as w:
        _time.sleep(0.3)
    assert w.expired and exits == []


def test_watchdog_is_cancelled_by_a_call_that_returns(monkeypatch):
    exits: list[int] = []
    monkeypatch.setattr(media, '_exit', lambda code: exits.append(code))
    with media._Watchdog('t.py::test_a', 0.2, {'PYTEST_XDIST_WORKER': 'gw1'}) as w:
        pass
    _time.sleep(0.4)
    assert not w.expired and exits == []
    with media._Watchdog('t.py::test_a', 0, {'PYTEST_XDIST_WORKER': 'gw1'}) as w:   # 0 disables
        _time.sleep(0.1)
    assert not w.expired
