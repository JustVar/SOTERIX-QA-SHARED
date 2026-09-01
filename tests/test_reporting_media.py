"""Media capture: the offline surface.

The full plugin needs Playwright and page objects, which the consumer repo
exercises through the shim. Here: the pure naming/config/off-value logic and
that configure() roots the evidence dirs where the caller says, since that
is the rule-5 change the extraction made.
"""
from __future__ import annotations

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
