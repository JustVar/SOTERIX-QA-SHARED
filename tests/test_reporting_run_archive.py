"""Unit tests for soterix_qa_shared.reporting.run_archive.

Pure-helper coverage plus the screenshot copy's gates (run on temp folders):
slug building, collision suffixing, prune selection, path-cap truncation,
the summary shape, index rendering, outcome classification, the guard
decisions, and the run.html hand-off to run_report. The full end-to-end
archive needs a finished pytest session with real report files, so it is
exercised by real runs, not here. The tests do not require
playwright, a browser, or network.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from soterix_qa_shared.reporting import run_archive as ra
from soterix_qa_shared.reporting import run_report as rr


@pytest.fixture(autouse=True)
def _c2c_path_rule():
    """The consumer's products shim configures this rule at import (c2c tests
    map to the Camera 2 Cloud product). The package test configures it the
    same way, so product resolution matches the consumer exactly."""
    from soterix_qa_shared.reporting import products
    products.configure_path_rules([('tests/ui/c2c/', '60W5')])
    yield
    products.configure_path_rules([])

# ---------------------------------------------------------------------------
# Slug building + cap
# ---------------------------------------------------------------------------

def test_slug_flattens_path_separators():
    """Path separators in a target flatten to dashes in the run slug."""
    assert ra._build_slug(['tests/ui/views']) == 'tests-ui-views'


def test_slug_joins_multiple_targets():
    """Several targets join into one dash-separated slug."""
    assert ra._build_slug(['tests/ui/media', 'tests/ui/maps']) == \
        'tests-ui-media-tests-ui-maps'


def test_slug_appends_the_markexpr():
    """The -m expression rides on the end of the run slug."""
    assert ra._build_slug(['tests/ui'], 'smoke and not slow') == \
        'tests-ui-smoke-and-not-slow'


def test_slug_empty_invocation_becomes_all():
    """A bare invocation with no target or markexpr is slugged as 'all'."""
    assert ra._build_slug([]) == 'all'
    assert ra._build_slug([], '') == 'all'


def test_slug_all_junk_becomes_all():
    """A target that sanitizes away entirely must still name a folder."""
    assert ra._build_slug(['///']) == 'all'


def test_slug_caps_at_40():
    """The run slug is cut at 40 characters."""
    assert len(ra._build_slug(['x' * 200])) == 40
    assert ra.SLUG_MAX == 40


def test_slug_matches_the_report_media_sanitizer():
    """Run folders and the files inside them follow one naming convention."""
    raw = 'tests/ui/views/test_views_basic.py::TestViews'
    expected = re.sub(r'[^\w.-]+', '-', raw).strip('-')[:40]
    assert ra._build_slug([raw]) == expected


# ---------------------------------------------------------------------------
# Collision suffixing
# ---------------------------------------------------------------------------

def test_unique_dir_returns_the_base_name_when_free(tmp_path):
    """A free run folder name is used as is, with no suffix."""
    assert ra._unique_dir(tmp_path, 'run') == tmp_path / 'run'


def test_unique_dir_appends_2_then_3(tmp_path):
    """A taken run folder name gets -2, then -3, on collision."""
    (tmp_path / 'run').mkdir()
    assert ra._unique_dir(tmp_path, 'run') == tmp_path / 'run-2'
    (tmp_path / 'run-2').mkdir()
    assert ra._unique_dir(tmp_path, 'run') == tmp_path / 'run-3'


# ---------------------------------------------------------------------------
# Prune selection (pure: given names, pick what goes)
# ---------------------------------------------------------------------------

def _run_name(i: int) -> str:
    return f'2026-08-{i:02d}_10-00-00__all'


def test_prune_keeps_the_newest_20():
    """Pruning keeps the newest 20 runs and lists the oldest first to go."""
    names = [_run_name(i) for i in range(1, 26)]  # 25 runs, day 25 newest
    prunable = ra._select_prunable(names)
    assert prunable == [_run_name(i) for i in range(5, 0, -1)]
    assert ra.KEEP_RUNS == 20


def test_size_prune_removes_oldest_over_the_byte_ceiling(tmp_path, monkeypatch):
    """The count caps let 20 near-duplicate livestream traces (~920 MB each)
    fill a disk. The byte ceiling removes oldest until under, keeping newest."""
    monkeypatch.setenv('QA_ARCHIVE_MAX_GB', '0.00001')     # 10 KB
    for h in (8, 9, 10):
        d = tmp_path / f'2026-09-01_{h:02d}-00-00__x'
        d.mkdir()
        (d / 'big.bin').write_bytes(b'0' * 8000)           # 3 x 8 KB > 10 KB
    ra._prune_by_size(tmp_path)
    left = sorted(p.name for p in tmp_path.iterdir() if p.is_dir())
    assert '2026-09-01_10-00-00__x' in left                # newest kept
    assert len(left) < 3                                   # something pruned


def test_a_single_over_cap_run_is_never_size_pruned(tmp_path, monkeypatch):
    """Deleting the only run because it is big would delete the evidence a
    developer is about to open."""
    monkeypatch.setenv('QA_ARCHIVE_MAX_GB', '0.000001')    # 1 KB
    d = tmp_path / '2026-09-01_10-00-00__x'
    d.mkdir()
    (d / 'huge.bin').write_bytes(b'0' * 9000)
    ra._prune_by_size(tmp_path)
    assert d.exists()


def test_prune_nothing_at_or_under_the_cap():
    """At or under the cap, nothing is pruned."""
    assert ra._select_prunable([_run_name(i) for i in range(1, 21)]) == []
    assert ra._select_prunable([]) == []


def test_prune_ignores_non_run_names():
    """index.html and hand-made folders must never be candidates."""
    names = ['index.html', 'keep-me', '2026-08-01_10-00', 'notes.txt',
             *[_run_name(i) for i in range(1, 25)]]
    prunable = ra._select_prunable(names)
    assert prunable == [_run_name(i) for i in range(4, 0, -1)]


def test_prune_pattern_requires_the_double_underscore():
    """Only names with the double underscore before the slug count as runs."""
    assert ra.RUN_DIR_RE.match('2026-08-01_10-00-00__views')
    assert not ra.RUN_DIR_RE.match('2026-08-01_10-00-00')
    assert not ra.RUN_DIR_RE.match('2026-08-01_10-00-00_views')


def test_prune_counts_per_target():
    """A morning of debug iterations on one file must never evict the last
    full-suite archive - only older runs of the same target."""
    suite = '2026-08-01_22-00-00__tests'
    debug = [f'2026-08-{i:02d}_10-00-00__tests-ui-views' for i in range(2, 27)]
    prunable = ra._select_prunable([suite, *debug])
    assert suite not in prunable
    # The debug slug is over its own cap of 20; its 5 oldest runs go.
    assert prunable == sorted(debug)[4::-1]


def test_prune_total_cap_spans_targets():
    """One-off targets each stay under their own cap forever - the total
    cap is what keeps them from accumulating without bound."""
    names = [f'2026-08-{i:02d}_10-00-00__t{i}' for i in range(1, 8)]
    prunable = ra._select_prunable(names, keep=20, total=5)
    assert prunable == [names[1], names[0]]
    assert ra.KEEP_RUNS_TOTAL == 100


# ---------------------------------------------------------------------------
# Path-cap truncation
# ---------------------------------------------------------------------------

def test_fit_dest_passes_short_paths_through(tmp_path):
    """A destination path under the cap is returned untouched."""
    dest = tmp_path / 'traces' / 'short-trace.zip'
    assert ra._fit_dest(dest) is dest


def test_fit_dest_truncates_the_stem_to_fit(tmp_path):
    """An overlong destination is cut to the cap, keeping folder and suffix."""
    dest = tmp_path / ('t' * 300 + '.zip')
    fitted = ra._fit_dest(dest)
    assert fitted is not None
    assert len(str(fitted)) == 250
    assert fitted.suffix == '.zip'
    assert str(fitted.parent) == str(tmp_path)


def test_fit_dest_none_when_the_directory_alone_is_too_long(tmp_path):
    """No destination fits when the folder alone is past the path cap."""
    deep = tmp_path / ('d' * 300)
    assert ra._fit_dest(deep / 'f.zip') is None


def test_fit_dest_keeps_distinct_sources_distinct(tmp_path):
    """Names that differ only in the middle must not fit to one destination -
    the later copy would silently overwrite the earlier one's evidence."""
    limit = len(str(tmp_path)) + 1 + 40 + len('.zip')  # stem room of 40
    twins = [ra._fit_dest(tmp_path / f'{"h" * 30}cam-{i}{"t" * 30}.zip', limit)
             for i in (3, 4)]
    assert twins[0] is not None
    assert twins[1] is not None
    assert twins[0].name != twins[1].name
    for fitted in twins:
        assert len(str(fitted)) <= limit


def test_fit_dest_keeps_the_stem_tail(tmp_path):
    """The tail is what tells near-twins apart to a human - the camera id,
    the PASSED/FAILED label - so the cut must not eat it."""
    fitted = ra._fit_dest(tmp_path / ('x' * 290 + '__FAILED.png'))
    assert fitted is not None
    assert fitted.name.endswith('__FAILED.png')


# ---------------------------------------------------------------------------
# Screenshot copy gates (freshness + path cap)
# ---------------------------------------------------------------------------

def test_copy_screenshots_skips_images_older_than_the_session(tmp_path, monkeypatch):
    """REPORT_SCREENSHOTS=off skips report_media's session-start wipe, so a
    prior run's images can still sit in the folder - mtime keeps them out."""
    src = tmp_path / 'shots'
    src.mkdir()
    stale = src / 'old__FAILED.png'
    stale.write_bytes(b'old')
    os.utime(stale, (1_000, 1_000))
    fresh = src / 'new__FAILED.png'
    fresh.write_bytes(b'new')
    os.utime(fresh, (2_000, 2_000))
    monkeypatch.setattr(ra, 'SCREENSHOT_DIR', src)

    dest = tmp_path / 'run'
    ra._copy_screenshots(1_500.0, dest)
    assert (dest / 'screenshots' / 'new__FAILED.png').is_file()
    assert not (dest / 'screenshots' / 'old__FAILED.png').exists()


def test_copy_screenshots_caps_the_destination_path(tmp_path, monkeypatch):
    """Long screenshot names under a long run folder must be shortened, not
    lost - default Windows refuses paths past 260 chars."""
    src = tmp_path / 'shots'
    src.mkdir()
    dest = tmp_path / 'run'
    # Sized to fit in the source folder but exceed the cap in the archive.
    room = ra.DEST_PATH_MAX - len(str(dest / 'screenshots')) - 1
    long_name = 'p' * (room + 10) + '.png'
    (src / long_name).write_bytes(b'png')
    monkeypatch.setattr(ra, 'SCREENSHOT_DIR', src)

    ra._copy_screenshots(0.0, dest)
    copied = list((dest / 'screenshots').glob('*.png'))
    assert len(copied) == 1
    assert copied[0].name != long_name
    # Same normalization as _fit_dest: resolve() would expand 8.3 short
    # names and measure a different string than the one that was capped.
    assert len(os.path.abspath(copied[0])) <= ra.DEST_PATH_MAX


# ---------------------------------------------------------------------------
# Summary shape
# ---------------------------------------------------------------------------

RECENT_EPOCH = 1790000000.0   # September 2026


def _offset_text(seconds: int) -> str:
    sign = '-' if seconds < 0 else '+'
    return f'{sign}{abs(seconds) // 3600:02d}:{abs(seconds) % 3600 // 60:02d}'


def test_utc_offset_is_this_machines_offset_at_that_moment():
    """The recorded offset is the offset that the platform gives for that moment, and the summary carries it."""
    import time
    here = _offset_text(time.localtime(RECENT_EPOCH).tm_gmtoff)
    assert ra._utc_offset(RECENT_EPOCH) == here
    summary = ra._build_summary(started=RECENT_EPOCH, ended=RECENT_EPOCH + 5, exitstatus=0, counts={},
                                target=[], markexpr='', keyword='', report_file='report.html')
    assert summary['utc_offset'] == here


@pytest.mark.parametrize(('seconds', 'text'), [
    (19800, '+05:30'), (-18000, '-05:00'), (-12600, '-03:30'), (0, '+00:00'), (None, '+00:00'), (50400, '+14:00'),
])
def test_utc_offset_writes_the_sign_the_hours_and_the_minutes(monkeypatch, seconds, text):
    """A zone east of UTC is '+', a zone west is '-', and a half hour stays a half hour on each side."""
    import time
    monkeypatch.setattr(time, 'localtime', lambda epoch: SimpleNamespace(tm_gmtoff=seconds))
    assert ra._utc_offset(RECENT_EPOCH) == text


@pytest.mark.parametrize('error', [OSError, OverflowError, ValueError])
def test_utc_offset_of_a_time_that_the_platform_cannot_place_is_empty(monkeypatch, error):
    """The platform refuses the time: the summary records no offset, and the run keeps its archive."""
    import time

    def refuse(epoch):
        raise error('out of range')

    monkeypatch.setattr(time, 'localtime', refuse)
    assert ra._utc_offset(RECENT_EPOCH) == ''


def test_summary_shape():
    """The summary carries exactly the schema keys with the run's facts."""
    counts = {'passed': 3, 'failed': 1, 'skipped': 2, 'errors': 0}
    summary = ra._build_summary(started=1000.0, ended=1090.5, exitstatus=1,
                                counts=counts, target=['tests/ui/views'],
                                markexpr='smoke', keyword='',
                                report_file='report.html')
    assert set(summary) == {'schema_version', 'started', 'ended', 'duration_s',
                            'exitstatus', 'counts', 'target', 'markexpr',
                            'keyword', 'report_file', 'advisories', 'tests',
                            'provenance', 'collected', 'not_run', 'not_run_count',
                            'one_cause', 'one_skip_reason', 'utc_offset'}
    assert summary['schema_version'] == 6
    # The offset of this machine at the start, or '' when the platform cannot place the time.
    assert summary['utc_offset'] == '' or re.match(r'^[+-]\d{2}:\d{2}$', summary['utc_offset'])
    assert (summary['collected'], summary['not_run'], summary['not_run_count']) == (None, [], 0)
    assert summary['tests'] == []
    assert summary['provenance'] == {}
    assert summary['duration_s'] == 90.5
    assert summary['exitstatus'] == 1
    assert summary['counts'] == counts
    assert summary['target'] == ['tests/ui/views']
    assert summary['markexpr'] == 'smoke'
    assert summary['keyword'] == ''
    assert summary['report_file'] == 'report.html'


def test_summary_counts_are_a_copy_with_fixed_keys():
    """The archive snapshot must not alias live state, and a sparse counts
    dict still yields every key the index renders."""
    counts = {'passed': 5}
    summary = ra._build_summary(started=0.0, ended=1.0, exitstatus=0,
                                counts=counts, target=[], markexpr='',
                                keyword='', report_file=None)
    assert summary['counts'] == {'passed': 5, 'failed': 0, 'skipped': 0, 'errors': 0}
    counts['passed'] = 99
    assert summary['counts']['passed'] == 5
    assert summary['report_file'] is None


# ---------------------------------------------------------------------------
# Index rendering (order + tolerated missing keys)
# ---------------------------------------------------------------------------

def _entry(name, summary=None, has_report=False):
    return {'name': name, 'summary': summary or {}, 'has_report': has_report}


def test_index_orders_newest_first():
    """The index lists runs newest first regardless of input order."""
    html = ra._render_index([
        _entry('2026-08-01_10-00-00__old'),
        _entry('2026-08-03_10-00-00__new'),
        _entry('2026-08-02_10-00-00__mid'),
    ])
    assert html.index('__new') < html.index('__mid') < html.index('__old')


def test_index_links_report_only_when_present():
    """The index links report.html only for runs that have one; the folder always."""
    html = ra._render_index([
        _entry('2026-08-01_10-00-00__a', has_report=True),
        _entry('2026-08-02_10-00-00__b', has_report=False),
    ])
    assert '<a href="2026-08-01_10-00-00__a/report.html">Raw pytest-html</a>' in html
    assert '2026-08-02_10-00-00__b/report.html' not in html
    # The folder link is always there - traces have no report to link.
    assert '<a href="2026-08-02_10-00-00__b/">Folder</a>' in html


def test_index_links_the_run_report_first_when_present():
    """run.html is where a reader starts, so its chip leads the Open cell,
    and the three chips carry three distinct labels."""
    html = ra._render_index([
        {'name': '2026-08-01_10-00-00__a', 'summary': {}, 'has_report': True,
         'has_run_report': True},
        {'name': '2026-08-02_10-00-00__b', 'summary': {}, 'has_report': True},
    ])
    cell_a = html[html.index('<span class="open"><a href="2026-08-01_10-00-00__a/run.html">'):]
    cell_a = cell_a[:cell_a.index('</span>')]
    assert cell_a.index('>Run Report<') < cell_a.index('>Raw pytest-html<') < cell_a.index('>Folder<')
    assert cell_a.count('<a ') == 3
    assert (ra.INDEX_LINK_RUN_REPORT, ra.INDEX_LINK_RAW_REPORT, ra.INDEX_LINK_FOLDER) == \
        ('Run Report', 'Raw pytest-html', 'Folder')
    assert '2026-08-02_10-00-00__b/run.html' not in html
    # The chips are boxed and spaced by CSS, not by words run together.
    assert '.open a { border:' in html
    assert 'gap: 0.4rem' in html


def test_index_wording_is_title_case():
    """The title, the heading, and the column headers read in Title Case."""
    html = ra._render_index([_entry('2026-08-01_10-00-00__a')])
    assert '<title>Test Run Archive</title>' in html
    assert '<h1>Test Run Archive</h1>' in html
    assert 'Test run archive' not in html
    headers = re.findall(r'<th[^>]*>([^<]+)</th>', html)
    assert headers == ['Started', 'Product', 'Target', 'Passed', 'Failed', 'Skipped', 'Errors',
                       'Outcome', 'Notes', 'Open']
    assert all(h == h[:1].upper() + h[1:] for h in headers)
    assert '>Exit<' not in html
    assert 'Advisories' not in html


def test_index_names_a_product_and_an_outcome_on_every_row(monkeypatch):
    """Product sits first after Started and is never blank: the run's own,
    else the one its targets name, else the mapping default. The outcome
    is a word with the exit code as its tooltip. The filter box matches a
    row on any of its words."""
    monkeypatch.delenv('QATOUCH_PROJECT_ID', raising=False)
    html = ra._render_index([
        _entry('2026-08-04_10-00-00__c2c', {'target': ['tests/ui/c2c'], 'exitstatus': 2}),
        _entry('2026-08-03_10-00-00__views', {'target': ['tests/ui/views'], 'exitstatus': 0,
                                              'provenance': {'product': 'NexaiQ-Neurosen'}}),
        _entry('2026-08-02_10-00-00__mix', {'target': ['tests/ui/views', 'tests/ui/c2c'],
                                            'exitstatus': 5}),
        _entry('2026-08-01_10-00-00__bare'),
    ], default_key='K5kB')
    rows = re.findall(r'<tr data-test="([^"]*)">(.*?)</tr>', html, flags=re.DOTALL)
    cells = [re.findall(r'<td>(.*?)</td>', row, flags=re.DOTALL) for _, row in rows]
    assert [c[1] for c in cells] == ['NexaiQ-Camera 2 Cloud', 'NexaiQ-Neurosen',
                                     'Mixed: NexaiQ-Camera 2 Cloud, NexiaQ-Mobile', 'NexiaQ-Mobile']
    assert [c[7] for c in cells] == ['<span title="exit code 2">Interrupted</span>',
                                     '<span title="exit code 0">Passed</span>',
                                     '<span title="exit code 5">No tests</span>', '-']
    assert 'id="filter"' in html
    assert "' runs match'" in html
    assert 'nexaiq-camera 2 cloud tests/ui/c2c interrupted' in rows[0][0]
    assert f'<th title="{ra.NOTES_TITLE}">Notes</th>' in html


def test_load_entries_flags_the_run_report(tmp_path):
    """Loaded entries flag whether the run folder holds a run.html."""
    (tmp_path / '2026-08-01_10-00-00__a').mkdir()
    (tmp_path / '2026-08-01_10-00-00__a' / 'run.html').write_text('x', encoding='utf-8')
    (tmp_path / '2026-08-02_10-00-00__b').mkdir()
    flags = {e['name']: e['has_run_report'] for e in ra._load_entries(tmp_path)}
    assert flags == {'2026-08-01_10-00-00__a': True, '2026-08-02_10-00-00__b': False}


def test_index_tolerates_an_empty_summary():
    """A folder from an older layout (no summary.json) still gets a row
    built from its name alone."""
    html = ra._render_index([_entry('2026-08-01_10-00-00__views')])
    assert '2026-08-01 10-00-00' in html  # timestamp from the folder name
    assert 'views' in html                # target from the folder name
    assert '<td>-</td>' in html           # unknown counts render as '-'


def test_index_renders_summary_values():
    """The index shows the summary's start time, target, and counts."""
    summary = {'started': '2026-08-01T10:00:00', 'exitstatus': 0,
               'counts': {'passed': 7, 'failed': 1, 'skipped': 2, 'errors': 0},
               'target': ['tests/ui/media']}
    html = ra._render_index([_entry('2026-08-01_10-00-00__media', summary)])
    assert '2026-08-01T10:00:00' in html
    assert 'tests/ui/media' in html
    for cell in ('<td>7</td>', '<td>1</td>', '<td>2</td>', '<td>0</td>'):
        assert cell in html


def test_index_shows_the_target_alone_even_for_an_older_run_with_a_tenant_label():
    """One server: the index once showed a tenant chip beside the target. A run
    archived with that label still lists, and the row reads the same as any other."""
    with_label = {'target': ['tests/ui'], 'provenance': {'env': 'qa', 'box': 'old-label'}}
    without = {'target': ['tests/ui'], 'provenance': {'env': 'qa'}}
    html = ra._render_index([_entry('2026-09-08_04-00-00__tests-ui', with_label),
                             _entry('2026-09-07_04-00-00__tests-ui', without)])
    rows = re.findall(r'<tr data-test="([^"]*)">(.*?)</tr>', html, flags=re.DOTALL)
    assert len(rows) == 2
    assert '<td>tests/ui</td>' in rows[0][1] and '<td>tests/ui</td>' in rows[1][1]
    assert 'class="box"' not in html and 'old-label' not in html


def test_index_has_a_caption_and_survives_no_runs():
    """An empty archive still renders the caption and a no-runs notice."""
    html = ra._render_index([])
    assert 'newest first' in html
    assert 'No archived runs' in html


# ---------------------------------------------------------------------------
# Outcome classification (summary counters)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(('when', 'outcome', 'counter'), [
    ('call', 'passed', 'passed'),
    ('call', 'failed', 'failed'),
    ('call', 'skipped', 'skipped'),
    ('setup', 'failed', 'errors'),    # a setup crash is an error, not a failure
    ('setup', 'skipped', 'skipped'),
])
def test_classify_counted_phases(when, outcome, counter):
    """Call outcomes and setup failures land in the matching counter."""
    assert ra._classify(when, outcome) == counter


@pytest.mark.parametrize(('when', 'outcome'), [
    ('setup', 'passed'),      # setup success is not a result yet
    ('teardown', 'passed'),
    ('call', 'rerun'),        # rerun outcomes are not final
    ('???', 'passed'),
])
def test_classify_ignored_phases(when, outcome):
    """Setup passes, clean teardowns and reruns count toward nothing."""
    assert ra._classify(when, outcome) is None


def test_classify_counts_what_pytest_counts():
    """A teardown failure is an error and a crashed worker's stand-in report
    (when='???') is a failure: pytest exits 1 on both, and until 09/18/2026
    this archive counted neither."""
    assert ra._classify('teardown', 'failed') == 'errors'
    assert ra._classify('???', 'failed') == 'failed'


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('value', ['off', 'OFF', 'Off', '0', 'false', 'no', ' off '])
def test_kill_switch_off_values(value):
    """RUN_ARCHIVE=off, in any spelling, disables the archive."""
    assert ra._archive_disabled({'RUN_ARCHIVE': value}) is True


@pytest.mark.parametrize('value', ['', 'on', '1', 'true', 'anything-else'])
def test_kill_switch_on_values(value):
    """Any RUN_ARCHIVE value other than an off word leaves the archive on."""
    assert ra._archive_disabled({'RUN_ARCHIVE': value}) is False


def test_kill_switch_defaults_to_on():
    """With RUN_ARCHIVE unset, the archive is on."""
    assert ra._archive_disabled({}) is False


# ---------------------------------------------------------------------------
# Archive guard decisions
# ---------------------------------------------------------------------------

class _FakeConfig:
    """Just enough of pytest Config for the guard and the archive: getoption
    plus the invocation targets in `args`."""

    def __init__(self, collect_only=False, args=('tests/ui',), htmlpath=None):
        self._collect_only = collect_only
        self.args = list(args)
        self._htmlpath = htmlpath

    def getoption(self, name, default=None):
        if name == '--collect-only':
            return self._collect_only
        if name == 'htmlpath':
            return self._htmlpath
        return default


def test_should_archive_in_a_plain_run():
    """A plain run with tests executed is archived."""
    assert ra._should_archive(_FakeConfig(), {}, tests_run=5) is True


def test_should_not_archive_during_collect_only():
    """run_suite's planning pass must never bury the real run's archive."""
    assert ra._should_archive(_FakeConfig(collect_only=True), {}, tests_run=5) is False


def test_should_not_archive_in_xdist_worker():
    """An xdist worker never archives; the controller does."""
    env = {'PYTEST_XDIST_WORKER': 'gw0'}
    assert ra._should_archive(_FakeConfig(), env, tests_run=5) is False


def test_should_not_archive_when_zero_tests_ran():
    """A run that executed no tests is not archived."""
    assert ra._should_archive(_FakeConfig(), {}, tests_run=0) is False


def test_should_not_archive_when_every_guard_trips():
    """Collect-only, an xdist worker, and zero tests together still refuse."""
    env = {'PYTEST_XDIST_WORKER': 'gw1'}
    assert ra._should_archive(_FakeConfig(collect_only=True), env, tests_run=0) is False


@pytest.mark.parametrize('args', [
    ['tests/plugins'],
    ['tests/tools'],
    ['tests/plugins', 'tests/tools'],
    ['tests/plugins/test_run_archive.py::test_slug_caps_at_40'],
    ['tests\\plugins\\test_run_archive.py'],
    ['tests/plugins/'],
    ['C:/repo/tests/tools/test_qatouch_client.py'],
])
def test_should_not_archive_a_unit_test_only_run(args):
    """The repo's own unit tests leave no UI evidence; a folder per plugin
    test run would bury the suite runs the archive exists for."""
    assert ra._unit_tests_only(args) is True
    assert ra._should_archive(_FakeConfig(args=args), {}, tests_run=40) is False


@pytest.mark.parametrize('args', [
    [],                                   # bare pytest: the whole suite
    ['tests'],
    ['tests/ui/views'],
    ['tests/plugins', 'tests/ui/views'],  # one UI target keeps the archive
    ['tests/ui/plugins_page/test_x.py'],  # a UI folder that merely contains the word
])
def test_should_archive_when_any_target_is_not_a_unit_test(args):
    """One UI target in the invocation is enough to keep the archive."""
    assert ra._unit_tests_only(args) is False
    assert ra._should_archive(_FakeConfig(args=args), {}, tests_run=5) is True


# ---------------------------------------------------------------- run.html hand-off

def _archive_on_temp(tmp_path, monkeypatch):
    """Point every live path at the temp folder and archive one fake run."""
    monkeypatch.setattr(ra, 'RUNS_DIR', tmp_path / 'runs')
    monkeypatch.setattr(ra, 'SCREENSHOT_DIR', tmp_path / 'shots')
    monkeypatch.setattr(ra, 'TRACE_DIR', tmp_path / 'artifacts')
    monkeypatch.setattr(ra, 'RESULTS_JSON', tmp_path / 'none.json')
    (tmp_path / 'shots').mkdir()
    (tmp_path / 'shots' / 't-x.py-test_a-chromium__FAILED.png').write_bytes(b'png')
    state = ra._RunState()
    state.started = 1_700_000_000.0
    state.ended = state.started + 12.5
    state.exitstatus = 1
    state.counts['failed'] = 1
    state.advisories = [{'test': 't/x.py::test_a[chromium]', 'phase': 'call',
                         'line': '[LPR] play_camera: drag landed nothing'}]
    ra._archive_run(_FakeConfig(args=['t/x.py']), state)
    folders = [p for p in (tmp_path / 'runs').iterdir() if p.is_dir()]
    assert len(folders) == 1
    return folders[0]


def test_archive_writes_the_run_report(tmp_path, monkeypatch):
    """The archive writes run.html with failures, screenshots, and advisories."""
    dest = _archive_on_temp(tmp_path, monkeypatch)
    page = (dest / 'run.html').read_text(encoding='utf-8')
    assert (dest / 'summary.json').is_file()
    assert (dest / 'advisories.txt').is_file()
    assert '<em class="tone-fail">1 of 1 failed.</em>' in page
    assert page.count('<details class="cause">') == 1
    assert 'screenshots/t-x.py-test_a-chromium__FAILED.png' in page
    assert '[LPR] play_camera: drag landed nothing' in page


def test_archive_survives_a_report_builder_failure(tmp_path, monkeypatch):
    """A page that cannot be built costs the page, never the copies."""
    def boom(run_dir):
        raise RuntimeError('builder down')
    monkeypatch.setattr(ra, 'build_run_report', boom)
    dest = _archive_on_temp(tmp_path, monkeypatch)
    assert (dest / 'summary.json').is_file()
    assert (dest / 'screenshots' / 't-x.py-test_a-chromium__FAILED.png').is_file()
    assert not (dest / 'run.html').exists()


# ---------------------------------------------------------------- advisories

class TestAdvisories:
    """The [VIEWS]/[LPR]/[EXPORT] marker lines ride into the archive."""

    CAPTURED = (
        "[AUTH] Loading cached session from .auth_state.json\n"
        "DEBUG expand_site_and_show_cameras: Clicked devices subtab\n"
        "[VIEWS] view drag: mouse drag did not consume a tile; trying JS fallback\n"
        "[VIEWS] view drag: mouse drag did not consume a tile; trying JS fallback\n"
        "  [EXPORT] completed in 14s (progress bar hidden)\n"
        "[LPR] play_camera: drag landed nothing\n"
        "[TRACE] Saved failure trace to reports/artifacts/x-trace.zip\n"
        "[VIEWS]\n"
    )

    def test_scan_keeps_only_tagged_lines_in_order(self):
        """Only tagged lines with text survive the advisory scan, in order."""
        assert ra.scan_advisories(self.CAPTURED) == [
            "[VIEWS] view drag: mouse drag did not consume a tile; trying JS fallback",
            "[EXPORT] completed in 14s (progress bar hidden)",
            "[LPR] play_camera: drag landed nothing",
        ]

    def test_scan_tolerates_empty_and_none(self):
        """Scanning empty or missing output yields no advisories."""
        assert ra.scan_advisories('') == []
        assert ra.scan_advisories(None) == []

    def test_summary_carries_advisories_and_defaults_empty(self):
        """The summary copies the advisory list and defaults it to empty."""
        base = {'started': 1000.0, 'ended': 1010.0, 'exitstatus': 0,
                    'counts': {'passed': 1}, 'target': ['t'], 'markexpr': '',
                    'keyword': '', 'report_file': None}
        assert ra._build_summary(**base)['advisories'] == []
        items = [{'test': 't::a', 'phase': 'call', 'line': '[LPR] x'}]
        out = ra._build_summary(**base, advisories=items)
        assert out['advisories'] == items
        assert out['advisories'] is not items  # a copy, never the live list

    def test_format_groups_lines_under_their_test(self):
        """advisories.txt groups each test's lines under its node id."""
        text = ra._format_advisories([
            {'test': 't::a', 'line': '[VIEWS] one'},
            {'test': 't::a', 'line': '[VIEWS] two'},
            {'test': 't::b', 'line': '[LPR] three'},
        ])
        assert text == 't::a\n  [VIEWS] one\n  [VIEWS] two\nt::b\n  [LPR] three\n'

    def test_index_shows_the_count_and_tolerates_missing_key(self):
        """The index shows each run's advisory count and blanks a missing key."""
        html = ra._render_index([
            {'name': '2026-08-21_10-00-00__a', 'has_report': False,
             'summary': {'advisories': [{'line': '[LPR] x'}, {'line': '[VIEWS] y'}]}},
            {'name': '2026-08-21_09-00-00__b', 'has_report': False, 'summary': {}},
        ])
        assert f'<th title="{ra.NOTES_TITLE}">Notes</th>' in html
        assert '<td>2</td>' in html
        assert html.index('__a') < html.index('__b')


# ---------------------------------------------------------------- per-test facts

class _FakePhaseReport:
    """Just enough of a pytest TestReport for record_phase."""

    def __init__(self, nodeid, when, outcome, duration=1.0, stdout='', stderr='',
                 longrepr=''):
        self.nodeid = nodeid
        self.when = when
        self.outcome = outcome
        self.duration = duration
        self.capstdout = stdout
        self.capstderr = stderr
        self.longreprtext = longrepr


MAPPED = 'tests/ui/admin/test_admin_general.py::TestAdminGeneral::test_tabs[chromium]'


def _write_mapping(tmp_path, mappings) -> Path:
    path = tmp_path / 'mapping.json'
    path.write_text(json.dumps({'version': 1, 'mappings': mappings}), encoding='utf-8')
    return path


class TestCaseMapping:
    """QA Touch case titles ride along when the node id is mapped."""

    def test_load_reads_key_and_title(self, tmp_path):
        """Loading the mapping keeps only rows with a case key, as key plus title."""
        path = _write_mapping(tmp_path, {
            MAPPED: {'case_key': 'VKbBPP', 'case_title': 'Verify the tabs', 'reviewed': True},
            'tests/x.py::test_no_key': {'case_title': 'orphan'},
            'tests/x.py::test_odd': 'not a row',
        })
        assert ra.load_case_mapping(path) == {
            MAPPED: {'case_key': 'VKbBPP', 'case_title': 'Verify the tabs'}}

    def test_load_tolerates_missing_and_malformed_files(self, tmp_path):
        """A missing or malformed mapping file loads as empty."""
        assert ra.load_case_mapping(tmp_path / 'nope.json') == {}
        bad = tmp_path / 'bad.json'
        bad.write_text('{not json', encoding='utf-8')
        assert ra.load_case_mapping(bad) == {}
        bad.write_text(json.dumps({'mappings': ['a', 'b']}), encoding='utf-8')
        assert ra.load_case_mapping(bad) == {}

    def test_lookup_exact_then_bare_then_none(self):
        """Lookup tries the exact node id, then the bare function, then gives up."""
        mapping = {MAPPED: {'case_key': 'K1', 'case_title': 'T1'},
                   'tests/y.py::test_bare': {'case_key': 'K2', 'case_title': ''}}
        assert ra.lookup_case(mapping, MAPPED) == {'case_key': 'K1', 'case_title': 'T1'}
        # A mapping keyed on the bare function still names the [chromium] variant.
        assert ra.lookup_case(mapping, 'tests/y.py::test_bare[chromium]') == {
            'case_key': 'K2', 'case_title': None}
        assert ra.lookup_case(mapping, 'tests/z.py::test_other') == {
            'case_key': None, 'case_title': None}
        assert ra.lookup_case({}, MAPPED) == {'case_key': None, 'case_title': None}


class TestProduct:
    """Each record names its product, and the run names one or a mix."""

    C2C = 'tests/ui/c2c/test_c2c_camera.py::TestCamera::test_reboot[chromium]'
    CORE = 'tests/ui/views/test_views_basic.py::TestViews::test_open[chromium]'

    def test_load_project_key_reads_the_top_level_key(self, tmp_path):
        """The mapping file's project_key loads; a missing file or key is None."""
        path = tmp_path / 'mapping.json'
        path.write_text(json.dumps({'version': 1, 'project_key': 'K5kB', 'mappings': {}}),
                        encoding='utf-8')
        assert ra.load_project_key(path) == 'K5kB'
        path.write_text(json.dumps({'version': 1, 'mappings': {}}), encoding='utf-8')
        assert ra.load_project_key(path) is None
        assert ra.load_project_key(tmp_path / 'nope.json') is None
        path.write_text('[1, 2]', encoding='utf-8')
        assert ra.load_project_key(path) is None

    def test_record_phase_names_the_product_by_precedence(self, monkeypatch):
        """Marker first, then the c2c path, then the mapping default."""
        monkeypatch.delenv('QATOUCH_PROJECT_ID', raising=False)
        state = ra._RunState()
        ra.record_phase(state, _FakePhaseReport(self.C2C, 'call', 'passed'),
                        marker_project='P0BK', project_key='K5kB')
        assert state.tests[self.C2C]['product'] == 'NexaiQ-Neurosen'
        state = ra._RunState()
        ra.record_phase(state, _FakePhaseReport(self.C2C, 'call', 'passed'), project_key='K5kB')
        assert state.tests[self.C2C]['product'] == 'NexaiQ-Camera 2 Cloud'
        ra.record_phase(state, _FakePhaseReport(self.CORE, 'call', 'passed'), project_key='K5kB')
        assert state.tests[self.CORE]['product'] == 'NexiaQ-Mobile'
        state = ra._RunState()
        ra.record_phase(state, _FakePhaseReport(self.CORE, 'call', 'passed'))
        assert state.tests[self.CORE]['product'] == 'NexaiQ-Core'

    def test_marker_project_reads_the_item(self):
        """The qatouch_project marker's first argument, or None without one."""
        class Item:
            def __init__(self, marker):
                self.marker = marker

            def get_closest_marker(self, name):
                return self.marker if name == 'qatouch_project' else None

        class Marker:
            args = ('60w8',)

        assert ra.marker_project(Item(Marker())) == '60w8'
        assert ra.marker_project(Item(None)) is None
        assert ra.marker_project(object()) is None

    def test_archive_records_one_product_or_a_mix(self, tmp_path, monkeypatch):
        """Provenance carries the single product, or "Mixed: A, B" sorted."""
        monkeypatch.delenv('QATOUCH_PROJECT_ID', raising=False)
        monkeypatch.setattr(ra, 'RUNS_DIR', tmp_path / 'runs')
        monkeypatch.setattr(ra, 'SCREENSHOT_DIR', tmp_path / 'shots')
        monkeypatch.setattr(ra, 'TRACE_DIR', tmp_path / 'artifacts')
        monkeypatch.setattr(ra, 'RESULTS_JSON', tmp_path / 'none.json')
        monkeypatch.setattr(ra, 'MAPPING_FILE', tmp_path / 'no-mapping.json')

        def archive(nodeids):
            state = ra._RunState()
            state.mapping = {}
            state.started, state.ended, state.exitstatus = 1_700_000_000.0, 1_700_000_010.0, 0
            for nodeid in nodeids:
                ra.record_phase(state, _FakePhaseReport(nodeid, 'call', 'passed'),
                                project_key=state.project_key())
            state.counts['passed'] = len(nodeids)
            ra._archive_run(_FakeConfig(args=['tests/ui']), state)
            dest = max(p for p in (tmp_path / 'runs').iterdir() if p.is_dir())
            return json.loads((dest / 'summary.json').read_text(encoding='utf-8'))

        single = archive([self.CORE])
        assert single['provenance']['product'] == 'NexaiQ-Core'
        assert single['tests'][0]['product'] == 'NexaiQ-Core'
        mixed = archive([self.CORE, self.C2C])
        assert mixed['provenance']['product'] == 'Mixed: NexaiQ-Camera 2 Cloud, NexaiQ-Core'
        assert [t['product'] for t in mixed['tests']] == ['NexaiQ-Core', 'NexaiQ-Camera 2 Cloud']


class TestLogFileName:
    def test_sanitizes_like_the_screenshot_stem(self):
        """The log file name is the screenshot stem plus .log."""
        nodeid = 'tests/ui/views/test_views_basic.py::TestViews::test_open[chromium]'
        assert ra.log_file_name(nodeid) == \
            'tests-ui-views-test_views_basic.py-TestViews-test_open-chromium.log'

    def test_caps_the_stem(self):
        """The log file stem is cut at 120 characters."""
        name = ra.log_file_name('tests/' + 'x' * 300)
        assert name.endswith('.log')
        assert len(name) == ra.LOG_NAME_MAX + len('.log')
        assert ra.LOG_NAME_MAX == 120


class TestRecordPhase:
    NODEID = 'tests/ui/views/test_views_drag_drop.py::TestDrag::test_drag_camera_to_tile[chromium]'

    def _state(self):
        state = ra._RunState()
        state.mapping = {self.NODEID: {'case_key': 'K9', 'case_title': 'Drag a camera'}}
        return state

    def test_builds_the_record_across_phases(self):
        """Phase reports build one record with title, case, outcome, and timings."""
        state = self._state()
        phases = [
            _FakePhaseReport(self.NODEID, 'setup', 'passed', 0.5, stdout='[AUTH] cached\n'),
            _FakePhaseReport(self.NODEID, 'call', 'failed', 12.25, stdout='[VIEWS] fallback\n',
                             stderr='warn\n', longrepr='E   assert False'),
            _FakePhaseReport(self.NODEID, 'teardown', 'passed', 0.25),
        ]
        for report in phases:
            ra.record_phase(state, report, doc='Verify a camera can be dragged.\n\n    More.',
                            mapping=state.mapping)
        rec = state.tests[self.NODEID]
        assert rec['title'] == 'Verify a camera can be dragged.'
        assert rec['module'] == 'ui/views/test_views_drag_drop.py'
        assert rec['case_key'] == 'K9'
        assert rec['case_title'] == 'Drag a camera'
        assert rec['outcome'] == 'failed'
        assert rec['phases'] == {'setup': 0.5, 'call': 12.25, 'teardown': 0.25}
        assert rec['duration_s'] == 13.0
        assert rec['log_file'] is None
        assert rec['error'] == 'assert False'
        captured = state.captured[self.NODEID]
        assert [c['phase'] for c in captured] == ['setup', 'call', 'teardown']
        assert captured[1]['stderr'] == 'warn\n'
        assert captured[1]['error'] == 'E   assert False'
        assert captured[0]['error'] == ''  # only a failed phase keeps its traceback

    @pytest.mark.parametrize(('when', 'outcome', 'expected'), [
        ('setup', 'failed', 'error'),
        ('setup', 'skipped', 'skipped'),
        ('call', 'passed', 'passed'),
        ('call', 'skipped', 'skipped'),
    ])
    def test_outcome_vocabulary(self, when, outcome, expected):
        """A setup failure records as error; skips and passes keep their names."""
        state = self._state()
        ra.record_phase(state, _FakePhaseReport(self.NODEID, when, outcome))
        assert state.tests[self.NODEID]['outcome'] == expected

    def test_error_line_is_kept_for_failures_only(self):
        """A failed call or setup records its first error line; a pass records None."""
        state = self._state()
        ra.record_phase(state, _FakePhaseReport(self.NODEID, 'setup', 'failed', 1.0,
                                                longrepr='x = 1\nE   fixture "api" not found\nE   more'))
        assert state.tests[self.NODEID]['outcome'] == 'error'
        assert state.tests[self.NODEID]['error'] == 'fixture "api" not found'
        state = self._state()
        ra.record_phase(state, _FakePhaseReport(self.NODEID, 'call', 'passed', 1.0))
        assert state.tests[self.NODEID]['error'] is None
        ra.record_phase(state, _FakePhaseReport(self.NODEID, 'teardown', 'failed', 1.0,
                                                longrepr='E   teardown boom'))
        # a pass whose teardown failed is an error, as pytest counts it
        assert state.tests[self.NODEID]['outcome'] == 'error'
        assert state.tests[self.NODEID]['error'] == 'teardown boom'
        assert state.tests[self.NODEID]['teardown_error'] == 'teardown boom'

    def test_a_failed_test_keeps_its_own_error_when_teardown_fails_too(self):
        """The call's failure is the test's story; the teardown error is kept beside it."""
        state = self._state()
        ra.record_phase(state, _FakePhaseReport(self.NODEID, 'call', 'failed', 1.0, longrepr='E   call boom'))
        ra.record_phase(state, _FakePhaseReport(self.NODEID, 'teardown', 'failed', 1.0, longrepr='E   teardown boom'))
        rec = state.tests[self.NODEID]
        assert (rec['outcome'], rec['error'], rec['teardown_error']) == ('failed', 'call boom', 'teardown boom')

    def test_a_crashed_worker_report_is_a_recorded_failure(self):
        """xdist's stand-in report has when='???': the record is a failure with its line, marked crashed."""
        state = self._state()
        ra.record_phase(state, _FakePhaseReport(self.NODEID, '???', 'failed', 0.0,
                                                longrepr="worker 'gw1' crashed while running 'x'"))
        rec = state.tests[self.NODEID]
        assert rec['outcome'] == 'failed'
        assert rec['crashed'] is True
        assert "worker 'gw1' crashed" in rec['error']

    def test_a_rerun_is_counted_on_the_record_and_keeps_its_first_error(self):
        """Passed on the second attempt: outcome passed, reruns 1, the first attempt's line kept."""
        state = self._state()
        ra.record_phase(state, _FakePhaseReport(self.NODEID, 'call', 'rerun', 1.0, longrepr='E   first boom'))
        ra.record_phase(state, _FakePhaseReport(self.NODEID, 'call', 'passed', 1.0))
        rec = state.tests[self.NODEID]
        assert (rec['outcome'], rec['reruns'], rec['rerun_error']) == ('passed', 1, 'first boom')

    def test_expected_failures_and_unexpected_passes_get_their_own_words(self):
        """pytest says skipped and passed; the record says xfailed and xpassed, with the xfail reason."""
        state = self._state()
        report = _FakePhaseReport(self.NODEID, 'call', 'skipped', 1.0, longrepr='[gw0] noise')
        report.wasxfail = 'the app returns 500 here'
        ra.record_phase(state, report)
        assert state.tests[self.NODEID]['outcome'] == 'xfailed'
        assert state.tests[self.NODEID]['reason'] == 'the app returns 500 here'
        state = self._state()
        report = _FakePhaseReport(self.NODEID, 'call', 'passed', 1.0)
        report.wasxfail = ''
        ra.record_phase(state, report)
        assert state.tests[self.NODEID]['outcome'] == 'xpassed'

    def test_title_falls_back_to_the_function_name(self):
        """Without a docstring, the record's title comes from the function name."""
        state = ra._RunState()
        state.mapping = {}
        ra.record_phase(state, _FakePhaseReport(self.NODEID, 'setup', 'passed'), doc=None)
        assert state.tests[self.NODEID]['title'] == 'Drag camera to tile'
        assert state.tests[self.NODEID]['case_key'] is None

    def test_capture_reads_the_phase_section_only(self):
        """pytest accumulates sections across phases, so capstdout on the
        call report repeats setup's output; the phase's section does not."""
        report = _FakePhaseReport(self.NODEID, 'call', 'passed', 1.0,
                                  stdout='[AUTH] cached\n[VIEWS] drag\n')
        report.sections = [('Captured stdout setup', '[AUTH] cached\n'),
                           ('Captured stdout call', '[VIEWS] drag\n'),
                           ('Captured stderr call', 'warn\n')]
        assert ra.phase_capture(report, 'stdout') == '[VIEWS] drag\n'
        assert ra.phase_capture(report, 'stderr') == 'warn\n'
        report.when = 'teardown'
        assert ra.phase_capture(report, 'stdout') == ''
        # No sections at all: the joined attribute is the only source.
        plain = _FakePhaseReport(self.NODEID, 'call', 'passed', stdout='x\n')
        assert ra.phase_capture(plain, 'stdout') == 'x\n'
        assert ra.phase_capture(plain, 'stderr') == ''

    def test_rerun_attempts_are_not_final(self):
        """A rerun attempt leaves no outcome; the final attempt sets it."""
        state = self._state()
        ra.record_phase(state, _FakePhaseReport(self.NODEID, 'call', 'rerun', 30.0))
        assert state.tests[self.NODEID]['outcome'] is None
        assert state.tests[self.NODEID]['phases'] == {}
        assert self.NODEID not in state.captured
        ra.record_phase(state, _FakePhaseReport(self.NODEID, 'call', 'passed', 8.0))
        assert state.tests[self.NODEID]['outcome'] == 'passed'
        assert state.tests[self.NODEID]['duration_s'] == 8.0

    def test_summary_copies_the_records(self):
        """The summary snapshots the test records so later edits do not leak in."""
        state = self._state()
        ra.record_phase(state, _FakePhaseReport(self.NODEID, 'call', 'passed', 2.0))
        summary = ra._build_summary(started=0.0, ended=5.0, exitstatus=0, counts={},
                                    target=[], markexpr='', keyword='', report_file=None,
                                    tests=list(state.tests.values()),
                                    provenance={'env': 'qa'})
        assert summary['tests'][0]['nodeid'] == self.NODEID
        assert summary['provenance'] == {'env': 'qa'}
        state.tests[self.NODEID]['phases']['teardown'] = 1.0
        assert 'teardown' not in summary['tests'][0]['phases']


class TestLogPolicy:
    """Only failures get a log file; everything else goes into run.log."""

    @pytest.mark.parametrize(('outcome', 'wants'), [
        ('failed', True), ('error', True), ('passed', False), ('skipped', False),
        ('xfailed', False), (None, False),
    ])
    def test_wants_log_file(self, outcome, wants):
        """A failed or errored test gets its own file; no other outcome does."""
        assert ra.wants_log_file(outcome) is wants

    def test_format_run_log_appends_sections_after_the_records(self):
        """run.log is the session's records, then one section per test without a file."""
        text = ra.format_run_log('12:00 INFO x: hello\n', ['t::a\nA\n(no output)\n', 't::b\nB\n'])
        assert text.startswith('12:00 INFO x: hello\n\n===== ')
        assert ra.RUN_LOG_SECTION_TITLE in text
        assert text.index('t::a\nA') < text.index('t::b\nB')
        assert ra.format_run_log('', []) == '(no logging records)\n'
        assert ra.format_run_log('rec\n', []) == 'rec\n'
        assert '=====' not in ra.format_run_log('rec\n', [])
        assert ra.format_run_log('', ['t::a\n']).startswith('\n=====')


class TestTestLog:
    """A file per failed test with every phase's captured output, in order."""

    def test_format_lists_each_phase(self):
        """The test log lists its header lines, then each phase's output in order."""
        rec = {'nodeid': 't/x.py::test_a', 'title': 'Do a thing', 'case_key': 'K1',
               'case_title': 'Case one'}
        phases = [
            {'phase': 'setup', 'outcome': 'passed', 'duration': 0.5, 'stdout': '[AUTH] ok\n'},
            {'phase': 'call', 'outcome': 'failed', 'duration': 3.0, 'stdout': '',
             'stderr': 'boom\n', 'error': 'E   assert 1 == 2'},
            {'phase': 'teardown', 'outcome': 'passed', 'duration': 0.1},
        ]
        text = ra.format_test_log(rec, phases)
        assert text.startswith('t/x.py::test_a\nDo a thing\nQA Touch K1: Case one\n')
        assert '=== setup: passed (0.50 s) ===\n--- stdout ---\n[AUTH] ok\n' in text
        assert '=== call: failed (3.00 s) ===\n--- stderr ---\nboom\n--- error ---\nE   assert 1 == 2' in text
        assert '=== teardown: passed (0.10 s) ===\n(no output)\n' in text

    def test_format_without_a_case_or_phases(self):
        """A record with no case and no phases logs just its id and title."""
        text = ra.format_test_log({'nodeid': 't::a', 'title': 'A', 'case_key': None}, [])
        assert text == 't::a\nA\n'

    def test_archive_writes_the_log_folder(self, tmp_path, monkeypatch):
        """log/<test>.log for the failed test only, plus run.log with the
        session's records and the passed test's output as a section; the
        summary record points at the file where there is one."""
        monkeypatch.setattr(ra, 'RUNS_DIR', tmp_path / 'runs')
        monkeypatch.setattr(ra, 'SCREENSHOT_DIR', tmp_path / 'shots')
        monkeypatch.setattr(ra, 'TRACE_DIR', tmp_path / 'artifacts')
        monkeypatch.setattr(ra, 'RESULTS_JSON', tmp_path / 'none.json')
        state = ra._RunState()
        state.mapping = {}
        state.started, state.ended, state.exitstatus = 1_700_000_000.0, 1_700_000_010.0, 1
        state.counts['passed'] = 1
        state.counts['failed'] = 1
        passed = 't/x.py::test_a[chromium]'
        failed = 't/x.py::test_b[chromium]'
        ra.record_phase(state, _FakePhaseReport(passed, 'setup', 'passed', 0.5, stdout='hi\n'))
        ra.record_phase(state, _FakePhaseReport(passed, 'call', 'passed', 2.0, stdout='[LPR] x\n'))
        ra.record_phase(state, _FakePhaseReport(failed, 'call', 'failed', 3.0, stdout='boom\n',
                                                longrepr='E   assert 1 == 2'))
        state.run_log.attach(tmp_path / 'live' / 'run.log')
        logging.getLogger('views_page').info('opened the page')
        state.run_log.detach()

        ra._archive_run(_FakeConfig(args=['t/x.py']), state)
        dest = next(p for p in (tmp_path / 'runs').iterdir() if p.is_dir())
        assert sorted(p.name for p in (dest / 'log').iterdir()) == \
            ['run.log', 't-x.py-test_b-chromium.log']
        failed_log = (dest / 'log' / 't-x.py-test_b-chromium.log').read_text(encoding='utf-8')
        assert 'boom\n' in failed_log
        assert 'E   assert 1 == 2' in failed_log
        run_log = (dest / 'log' / 'run.log').read_text(encoding='utf-8')
        assert run_log.index('opened the page') < run_log.index(ra.RUN_LOG_SECTION_TITLE)
        assert f'{passed}\nA\n' in run_log
        assert 'hi\n' in run_log
        assert '[LPR] x' in run_log
        assert 'boom' not in run_log  # the failure's output lives in its own file
        summary = json.loads((dest / 'summary.json').read_text(encoding='utf-8'))
        by_id = {t['nodeid']: t for t in summary['tests']}
        assert by_id[passed]['log_file'] is None
        assert by_id[passed]['error'] is None
        assert by_id[passed]['title'] == 'A'
        assert by_id[failed]['log_file'] == 'log/t-x.py-test_b-chromium.log'
        assert by_id[failed]['error'] == 'assert 1 == 2'
        assert set(summary['provenance']) == {'env', 'base_url_host', 'git_commit',
                                              'playwright', 'pytest_playwright', 'browser',
                                              'browser_channel', 'product', 'host', 'operator',
                                              'account', 'browser_version', 'browser_latest',
                                              'camera', 'camera_reason'}
        page = (dest / 'run.html').read_text(encoding='utf-8')
        assert 'href="log/t-x.py-test_b-chromium.log"' in page
        assert 'href="log/t-x.py-test_a-chromium.log"' not in page
        assert '<a href="log/run.log">run.log</a>' in page
        # A passed test has no log file of its own, so the page says where its output is.
        assert 'captured output of passed and skipped tests is in' in page

    def test_archive_writes_run_log_without_a_session_log(self, tmp_path, monkeypatch):
        """No live run.log (the handler never attached) still yields a
        run.log with the passed tests' sections."""
        monkeypatch.setattr(ra, 'RUNS_DIR', tmp_path / 'runs')
        monkeypatch.setattr(ra, 'SCREENSHOT_DIR', tmp_path / 'shots')
        monkeypatch.setattr(ra, 'TRACE_DIR', tmp_path / 'artifacts')
        monkeypatch.setattr(ra, 'RESULTS_JSON', tmp_path / 'none.json')
        state = ra._RunState()
        state.mapping = {}
        state.started, state.ended, state.exitstatus = 1_700_000_000.0, 1_700_000_010.0, 0
        state.counts['passed'] = 1
        ra.record_phase(state, _FakePhaseReport('t/x.py::test_a', 'call', 'passed', 2.0, stdout='hi\n'))
        ra._archive_run(_FakeConfig(args=['t/x.py']), state)
        dest = next(p for p in (tmp_path / 'runs').iterdir() if p.is_dir())
        assert [p.name for p in (dest / 'log').iterdir()] == ['run.log']
        assert 'hi\n' in (dest / 'log' / 'run.log').read_text(encoding='utf-8')


class TestRunLog:
    def test_attach_writes_info_records_and_detach_restores(self, tmp_path):
        """Attaching writes INFO records to run.log; detaching restores the root logger."""
        root = logging.getLogger()
        before_level = root.level
        before_handlers = list(root.handlers)
        run_log = ra._RunLog()
        run_log.attach(tmp_path / 'run.log')
        try:
            assert root.getEffectiveLevel() <= logging.INFO
            logging.getLogger('report_media').info('cleared 3 screenshots')
            logging.getLogger('report_media').debug('not at INFO')
        finally:
            run_log.detach()
        text = (tmp_path / 'run.log').read_text(encoding='utf-8')
        assert 'INFO report_media: cleared 3 screenshots' in text
        assert 'not at INFO' not in text
        assert root.level == before_level
        assert root.handlers == before_handlers
        run_log.detach()  # a second detach is harmless

    def test_unit_test_runs_never_start_a_run_log(self):
        """Same guard as the archive: the repo's own tests leave no evidence."""
        assert ra._process_may_archive(_FakeConfig(args=['tests/plugins']), {}) is False
        assert ra._process_may_archive(_FakeConfig(collect_only=True), {}) is False
        assert ra._process_may_archive(_FakeConfig(), {'PYTEST_XDIST_WORKER': 'gw0'}) is False
        assert ra._process_may_archive(_FakeConfig(), {}) is True


def test_index_links_health_only_when_it_exists(tmp_path):
    """The index links health.html only when the file exists, as a chip
    labelled Health in the same style as the Open column."""
    entries = [_entry('2026-08-01_10-00-00__a')]
    assert 'href="health.html"' not in ra._render_index(entries)
    assert 'run.py health' in ra._render_index(entries)
    with_health = ra._render_index(entries, has_health=True)
    assert '<span class="open"><a href="health.html">Health</a></span>' in with_health
    assert 'Health summary' not in with_health
    (tmp_path / '2026-08-01_10-00-00__a').mkdir()
    ra._write_index(tmp_path)
    assert 'href="health.html"' not in (tmp_path / 'index.html').read_text(encoding='utf-8')
    (tmp_path / 'health.html').write_text('x', encoding='utf-8')
    ra._write_index(tmp_path)
    assert 'href="health.html"' in (tmp_path / 'index.html').read_text(encoding='utf-8')


def test_record_phase_keeps_a_skips_reason():
    """A skipped test's record carries its reason (the server's `without` map, the
    stream guard, require_items all say why); a passed test's reason stays None."""
    state = ra._RunState()
    report = _FakePhaseReport('tests/ui/x.py::test_a[chromium]', 'setup', 'skipped')
    report.longrepr = ('tests/ui/x.py', 12, "Skipped: this server is without 'lpr': no plate camera")
    ra.record_phase(state, report)
    rec = state.tests['tests/ui/x.py::test_a[chromium]']
    assert rec['outcome'] == 'skipped'
    assert rec['reason'] == "this server is without 'lpr': no plate camera"
    assert rec['error'] is None
    passed = _FakePhaseReport('tests/ui/x.py::test_b[chromium]', 'call', 'passed')
    ra.record_phase(state, passed)
    assert state.tests['tests/ui/x.py::test_b[chromium]']['reason'] is None


def test_skip_reason_reads_the_tuple_or_the_text():
    assert ra.skip_reason(SimpleNamespace(longrepr=('t.py', 1, 'Skipped: the reason'), longreprtext='x')) == 'the reason'
    assert ra.skip_reason(SimpleNamespace(longrepr=None, longreprtext='Skipped:  spaced   out  ')) == 'spaced out'
    assert ra.skip_reason(SimpleNamespace(longrepr=None, longreprtext='')) == ''


# ---------------------------------------------------------------- who ran it, on which Chrome

def _bare_config():
    return SimpleNamespace(getoption=lambda name, default=None: default)


def test_provenance_names_the_machine_the_operator_and_the_browser(monkeypatch, tmp_path):
    """A scheduled run and a laptop run land in one archive: the record
    says which machine ran it, as whom, on which Chrome, and whether
    Google's stable is newer. The version is written once, by the first
    page fixture to know it; a later writer does not overwrite it. A BOX
    in the environment is not read: one server has no tenant to name."""
    monkeypatch.setattr(ra, 'BROWSER_VERSION_FILE', tmp_path / '.browser_version')
    ra.record_browser_version('153.0.8010.36', 'chrome')
    ra.record_browser_version('999.0', 'chrome')
    monkeypatch.setattr(ra.socket, 'gethostname', lambda: 'runner-1')
    monkeypatch.setattr(ra.getpass, 'getuser', lambda: 'qa')
    monkeypatch.setattr(ra, 'latest_stable_browser', lambda timeout_s=4.0: '153.0.8010.47')
    prov = ra._provenance(_bare_config(), {'BOX': 'stale'})
    assert (prov['host'], prov['operator']) == ('runner-1', 'qa')
    assert 'box' not in prov
    assert (prov['browser_version'], prov['browser_latest']) == ('153.0.8010.36', '153.0.8010.47')
    assert prov['account'] is None  # no env module in this package: a label, not an error


def test_provenance_asks_for_the_latest_only_when_a_browser_was_recorded(monkeypatch, tmp_path):
    """A session that opened no browser never waits on the network."""
    monkeypatch.setattr(ra, 'BROWSER_VERSION_FILE', tmp_path / 'missing')
    monkeypatch.setattr(ra, 'latest_stable_browser', lambda timeout_s=4.0: pytest.fail('asked'))
    prov = ra._provenance(_bare_config(), {})
    assert (prov['browser_version'], prov['browser_latest']) == (None, None)
    ra.record_browser_version(None)
    assert not (tmp_path / 'missing').exists()


def test_latest_stable_browser_reads_the_first_version_and_tolerates_the_rest(monkeypatch):
    """Google's answer lists the newest first; no network, no answer, no crash."""
    import io

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(ra.urllib.request, 'urlopen',
                        lambda url, timeout: Resp(b'{"versions": [{"version": "153.0.8010.47"}, {"version": "153.0.8010.36"}]}'))
    assert ra.latest_stable_browser() == '153.0.8010.47'
    monkeypatch.setattr(ra.urllib.request, 'urlopen', lambda url, timeout: Resp(b'{"versions": []}'))
    assert ra.latest_stable_browser() is None

    def down(url, timeout):
        raise OSError('no route')

    monkeypatch.setattr(ra.urllib.request, 'urlopen', down)
    assert ra.latest_stable_browser() is None


def test_index_shows_the_machine_beside_the_target_and_a_runners_line():
    """A row carries its machine after its target; the page opens with each
    machine's Chrome from its newest run, and says when stable is newer."""
    runner_run = {'target': ['tests/ui'], 'provenance': {
        'host': 'runner-1', 'account': 'qa-account', 'operator': 'qa',
        'browser': 'chromium', 'browser_channel': 'chrome',
        'browser_version': '153.0.8010.36', 'browser_latest': '153.0.8010.47'}}
    older_runner_run = {'target': ['tests/ui'], 'provenance': {
        'box': 'old-label', 'host': 'runner-1', 'browser_version': '152.0.1', 'browser_latest': '152.0.1'}}
    laptop_run = {'target': ['tests/ui/views'], 'provenance': {
        'host': 'QA-LAPTOP', 'operator': 'Tester',
        'browser_channel': 'chrome', 'browser_version': '152.0.7977.83', 'browser_latest': '152.0.7977.83'}}
    html = ra._render_index([_entry('2026-09-17_19-10-45__tests-ui', runner_run),
                             _entry('2026-09-16_04-00-00__tests-ui', older_runner_run),
                             _entry('2026-09-17_12-00-00__tests-ui-views', laptop_run),
                             _entry('2026-09-15_04-00-00__tests-ui', {'target': ['tests/ui']})])
    assert '<td>tests/ui <code class="host" title="ran on runner-1">runner-1</code></td>' in html
    assert '<code class="host" title="ran on QA-LAPTOP">QA-LAPTOP</code>' in html
    runners = html.split('<p class="runners">')[1].split('</p>')[0]
    assert runners.startswith('Runners, by their newest run: ')
    assert ('<code class="host">QA-LAPTOP</code> Chrome 152.0.7977.83 (current stable); '
            '<code class="host">runner-1</code> Chrome 153.0.8010.36 '
            '<span class="behind">(stable 153.0.8010.47 is out: update it)</span>.') in runners
    assert 'qa-account' in html and 'tester' in html  # the filter words carry account and operator
    assert '<p class="runners">' not in ra._render_index([_entry('2026-09-15_04-00-00__tests-ui', {'target': ['tests/ui']})])


# ---------------------------------------------------------------- a run that stopped early, and one cause

def _summary_for(tests, collected=None):
    return ra._build_summary(started=1000.0, ended=1010.0, exitstatus=1,
                             counts={'passed': 0, 'failed': len(tests), 'skipped': 0, 'errors': 0},
                             target=['tests/ui'], markexpr='', keyword='', report_file=None,
                             tests=tests, collected=collected)


def test_a_run_that_stopped_early_says_how_much_never_ran():
    """Two of fifteen ran: the summary names the thirteen that did not, instead of reading as a run of two."""
    collected = [f't.py::test_{i}' for i in range(15)]
    tests = [{'nodeid': 't.py::test_0', 'outcome': 'passed'}, {'nodeid': 't.py::test_1', 'outcome': 'failed', 'error': 'boom'}]
    summary = _summary_for(tests, collected)
    assert (summary['collected'], summary['not_run_count']) == (15, 13)
    assert summary['not_run'][0] == 't.py::test_2'
    assert 'collected tests did not run' in rr.run_verdict(1, 1, 0, not_run=13)


def test_one_cause_names_the_error_most_failures_share():
    """Five failures with one signature (numbers and quotes differ) are one cause; four are not."""
    def fail(i, line):
        return {'nodeid': f't.py::test_{i}', 'outcome': 'failed', 'error': line}
    same = [fail(i, f"Page.goto: net::ERR_CONNECTION_REFUSED at https://x/auth/login after {i}000 ms") for i in range(5)]
    cause = _summary_for(same + [fail(9, 'other')])['one_cause']
    assert (cause['count'], cause['of'], cause['first']) == (5, 6, 't.py::test_0')
    assert 'ERR_CONNECTION_REFUSED' in cause['line']
    assert _summary_for(same[:4])['one_cause'] is None
    skips = [{'nodeid': f't.py::s{i}', 'outcome': 'skipped', 'reason': 'the camera streams H.265 and this browser cannot decode it'}
             for i in range(21)]
    assert _summary_for(skips)['one_skip_reason']['count'] == 21
    assert _summary_for(skips[:20])['one_skip_reason'] is None


def test_the_summary_counts_tests_not_reports():
    """A pass whose teardown failed is two reports and one test: the counts follow the records."""
    state = ra._RunState()
    state.counts.update({'passed': 3, 'failed': 7, 'errors': 1, 'skipped': 1})
    for i, outcome in enumerate(['passed', 'passed', 'error', 'xfailed', 'xpassed', 'failed', None]):
        state.tests[f't::{i}'] = {'nodeid': f't::{i}', 'outcome': outcome}
    assert ra._counts_of(state) == {'passed': 3, 'failed': 1, 'skipped': 1, 'errors': 1}
    empty = ra._RunState()
    empty.counts.update({'passed': 2})
    assert ra._counts_of(empty)['passed'] == 2


def test_error_line_skips_the_xdist_worker_banner():
    """A crashed worker's report leads with '[gw1] win32 -- Python ...'; the crash line is the one kept."""
    banner = "[gw1] win32 -- Python 3.14.6 C:/venv/python.exe"
    crash = "worker 'gw1' crashed while running 't::x'"
    assert rr.error_line(banner + chr(10) + crash) == crash
    assert rr.error_line("x = 1" + chr(10) + "E   assert 1 == 2") == 'assert 1 == 2'


# ---------------------------------------------------------------- a run that was not archived says so

def test_an_archive_failure_is_loud_and_leaves_its_reason(monkeypatch, tmp_path, capsys):
    """stderr names the error and the free space; the marker beside run.log holds the same line;
    a later good archive removes the marker."""
    monkeypatch.setattr(ra, 'RUN_LOG', tmp_path / 'logs' / 'run.log')
    monkeypatch.setattr(ra, 'RUNS_DIR', tmp_path / 'reports' / 'runs')
    ra._say_archive_failed(OSError(28, 'No space left on device'))
    err = capsys.readouterr().err
    assert 'THIS RUN WAS NOT ARCHIVED: OSError' in err
    assert 'No space left on device' in err
    assert 'MB)' in err
    marker = tmp_path / 'logs' / 'archive_failed.txt'
    assert 'THIS RUN WAS NOT ARCHIVED' in marker.read_text(encoding='utf-8')
    ra._clear_archive_failure()
    assert not marker.exists()


def test_a_nearly_full_disk_warns_at_session_start(monkeypatch, tmp_path, capsys):
    """Under 1 GB free where the reports go: one stderr line; plenty of space: silence."""
    monkeypatch.setattr(ra, 'RUNS_DIR', tmp_path)
    monkeypatch.setattr(ra, '_free_mb', lambda path: 120)
    ra._warn_low_disk()
    assert 'only 120 MB free' in capsys.readouterr().err
    monkeypatch.setattr(ra, '_free_mb', lambda path: 50_000)
    ra._warn_low_disk()
    assert capsys.readouterr().err == ''



def test_the_known_defect_provider_is_asked_per_test_and_cannot_break_the_archive():
    """The consumer's lookup answers {since, what} or None; a lookup that
    raises, or answers junk, leaves the record unlabelled and the run alive."""
    from soterix_qa_shared.reporting import run_archive as ra
    try:
        ra.configure_known_defects(lambda nodeid: {'since': '09/12', 'what': 'CSP'} if 'save_view' in nodeid else None)
        assert ra.known_defect_for('tests/ui/views/test_views_basic.py::test_save_view[chromium]') == {'since': '09/12', 'what': 'CSP'}
        assert ra.known_defect_for('tests/ui/views/test_views_basic.py::test_other[chromium]') is None
        ra.configure_known_defects(lambda nodeid: {'since': '09/12'})   # no what: not a label
        assert ra.known_defect_for('x::test_y') is None
        ra.configure_known_defects(lambda nodeid: 'CSP')               # not a dict
        assert ra.known_defect_for('x::test_y') is None
        def boom(nodeid):
            raise RuntimeError('yaml on fire')
        ra.configure_known_defects(boom)
        assert ra.known_defect_for('x::test_y') is None
        ra.configure_known_defects(None)
        assert ra.known_defect_for('x::test_y') is None
    finally:
        ra.configure_known_defects(None)


def test_the_known_defect_lookup_hands_the_error_over_and_still_serves_a_one_argument_provider():
    """A defect that hits by error signature can only be recognised once the failure has an
    error line, so the lookup is asked again then, with it. A provider written before that
    took the nodeid alone, and must keep working."""
    from soterix_qa_shared.reporting import run_archive as ra
    seen = []
    try:
        ra.configure_known_defects(lambda nodeid, error: (seen.append((nodeid, error)) or ({'what': 'search API'} if error and 'no search job' in error else None)))
        assert ra.known_defect_for('t.py::test_a') is None
        assert ra.known_defect_for('t.py::test_a', 'Search: no search job was sent within 0 s') == {'what': 'search API'}
        assert seen == [('t.py::test_a', None), ('t.py::test_a', 'Search: no search job was sent within 0 s')]
        ra.configure_known_defects(lambda nodeid: {'what': 'by name'})
        assert ra.known_defect_for('t.py::test_a', 'any error') == {'what': 'by name'}, 'the old shape still answers'
    finally:
        ra.configure_known_defects(None)
