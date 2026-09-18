"""Unit tests for soterix_qa_shared.reporting.run_report.

Pure-part coverage on temp folders: file-name parsing, test-to-file
matching (exact, digest-shortened, prefix, slugified), the trace digest
on a synthetic trace.zip, the three result sources and their merge, the
page's section order and evidence links, the verdict sentence, the
plain-language delta note, the error signature and failure grouping, the
history strip facts and flaky rate, tolerance of missing files and keys,
and the CLI. No browser, no network.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import zipfile
from pathlib import Path

import pytest

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

NODEID = 'tests/ui/views/test_views_basic.py::TestViews::test_open[chromium]'
SAFE = 'tests-ui-views-test_views_basic.py-TestViews-test_open-chromium'


def _digest(stem: str) -> str:
    return hashlib.sha256(stem.encode('utf-8')).hexdigest()[:8]


def _shorten(stem: str, keep_head: int, tail: int = 20) -> str:
    """Rebuild run_archive._fit_dest's shape: <head>-<digest>-<tail>."""
    return f'{stem[:keep_head]}-{_digest(stem)}-{stem[-tail:]}'


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

def test_safe_name_matches_the_report_media_sanitizer():
    """The report's safe name matches report_media's sanitizer at both caps."""
    assert rr._safe_name(NODEID, rr.SAFE_NAME_MAX) == SAFE
    long = 'tests/' + 'x' * 300
    assert len(rr._safe_name(long, rr.SAFE_NAME_MAX)) == 120
    assert len(rr._safe_name(long, rr.TRACE_NAME_MAX)) == 150
    assert rr._safe_name(long, 120) == re.sub(r'[^\w.-]+', '-', long).strip('-')[:120]


@pytest.mark.parametrize(('stem', 'suffix', 'label', 'index'), [
    (f'{SAFE}__PASSED', 'PASSED', None, None),
    (f'{SAFE}__FAILED', 'FAILED', None, None),
    (f'{SAFE}__SETUP_FAILED', 'SETUP_FAILED', None, None),
    (f'{SAFE}__FAILED__mid-drag-2', 'FAILED', 'mid-drag', '2'),
    (f'{SAFE}__SKIPPED__mid-drag-1', 'SKIPPED', 'mid-drag', '1'),
])
def test_parse_screenshot_name(stem, suffix, label, index):
    """A screenshot stem parses into test, suffix, and an optional frame label."""
    parsed = rr.parse_screenshot_name(stem)
    assert parsed == {'test': SAFE, 'suffix': suffix, 'label': label, 'index': index}


def test_parse_screenshot_name_rejects_unlabeled_stems():
    """A stem without an outcome suffix is not a screenshot name."""
    assert rr.parse_screenshot_name('random-image') is None
    assert rr.parse_screenshot_name(SAFE) is None


@pytest.mark.parametrize(('relpath', 'test', 'kind', 'nested'), [
    (f'{SAFE}-trace.zip', SAFE, 'trace', False),
    (f'{SAFE}-video.webm', SAFE, 'video', False),
    (f'{SAFE}-login-failure-trace.zip', SAFE, 'login-failure-trace', False),
    ('tests-ui-views-test-views-basic-py-testviews-test-open-chromium/trace.zip',
     'tests-ui-views-test-views-basic-py-testviews-test-open-chromium', 'trace', True),
    ('some-dir/video.webm', 'some-dir', 'video', True),
    ('some-dir/test-failed-1.png', 'some-dir', 'screenshot', True),
])
def test_parse_artifact_name(relpath, test, kind, nested):
    """Trace, video, and nested artifact paths parse into test and kind."""
    assert rr.parse_artifact_name(relpath) == {'test': test, 'kind': kind, 'nested': nested}


def test_parse_artifact_name_rejects_other_files():
    """Files that are not traces, videos, or screenshots do not parse."""
    assert rr.parse_artifact_name('notes.txt') is None
    assert rr.parse_artifact_name('plain.zip') is None


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

TWIN_A = 'tests/ui/c2c/test_cams.py::TestCams::test_reboot[camera-3]'
TWIN_B = 'tests/ui/c2c/test_cams.py::TestCams::test_reboot[camera-4]'


def _safes(*nodeids: str, cap: int = rr.SAFE_NAME_MAX) -> dict[str, str]:
    return {rr._safe_name(n, cap): n for n in nodeids}


def test_match_exact():
    """An exact safe-name match resolves straight to its node id."""
    assert rr.match_test(SAFE, _safes(NODEID, TWIN_A)) == NODEID


def test_match_digest_shortened_screenshot_tells_twins_apart():
    """Twins differ only past the cut; the recomputed digest picks the right one."""
    safes = _safes(TWIN_A, TWIN_B)
    for nodeid in (TWIN_A, TWIN_B):
        full = f'{rr._safe_name(nodeid, 120)}__PASSED'
        stem = _shorten(full, 30)
        parsed = rr.parse_screenshot_name(stem)
        assert parsed is not None
        assert rr.match_test(parsed['test'], safes,
                             lambda safe, s=parsed['suffix']: f'{safe}__{s}') == nodeid


def test_match_digest_shortened_mid_drag_frame():
    """A digest-shortened mid-drag frame still resolves to its test."""
    safes = _safes(NODEID)
    full = f'{SAFE}__PASSED__mid-drag-1'
    parsed = rr.parse_screenshot_name(_shorten(full, 40))
    assert parsed is not None
    assert parsed['label'] == 'mid-drag'
    frame = f"__{parsed['label']}-{parsed['index']}"
    assert rr.match_test(parsed['test'], safes,
                         lambda safe, s=parsed['suffix'], f=frame: f'{safe}__{s}{f}') == NODEID


def test_match_digest_shortened_trace():
    """A digest-shortened trace name resolves to the right twin."""
    safes = _safes(TWIN_A, TWIN_B, cap=rr.TRACE_NAME_MAX)
    full = f'{rr._safe_name(TWIN_B, 150)}-trace'
    parsed = rr.parse_artifact_name(_shorten(full, 30) + '.zip')
    assert parsed is not None
    assert rr.match_test(parsed['test'], safes, lambda safe: f'{safe}-trace') == TWIN_B


def test_match_stale_digest_falls_back_to_head_and_tail():
    """A digest from another layout still resolves when only one test fits."""
    safes = _safes(NODEID, TWIN_A)
    stem = f'{SAFE[:30]}-00000000-{SAFE[-5:]}'
    assert rr.match_test(stem, safes, lambda safe: safe) == NODEID


def test_match_longest_common_prefix():
    """A truncated or extended stem resolves by longest common prefix."""
    safes = _safes(NODEID, TWIN_A)
    assert rr.match_test(SAFE[:40], safes) == NODEID
    assert rr.match_test(f'{SAFE}-extra-suffix', safes) == NODEID


def test_match_slugified_folder_name():
    """pytest-playwright names its per-test folder with python-slugify."""
    slug = 'tests-ui-views-test-views-basic-py-testviews-test-open-chromium'
    assert rr.match_test(slug, _safes(NODEID, TWIN_A)) == NODEID


def test_match_none_when_nothing_is_related():
    """An unrelated stem, or an empty candidate set, matches nothing."""
    assert rr.match_test('zzz-unrelated', _safes(NODEID, TWIN_A)) is None
    assert rr.match_test('anything', {}) is None


# ---------------------------------------------------------------------------
# Trace digest
# ---------------------------------------------------------------------------

def _trace_events() -> list[dict]:
    events = [{'type': 'context-options', 'version': 8}]
    for i in range(1, 21):
        events.append({'type': 'before', 'callId': f'call@{i}', 'class': 'Frame',
                       'method': 'click', 'params': {'selector': f'#btn-{i}'}})
        events.append({'type': 'after', 'callId': f'call@{i}'})
    events.append({'type': 'frame-snapshot',
                   'snapshot': {'frameUrl': 'https://app.example/views'}})
    events.append({'type': 'before', 'callId': 'call@99', 'apiName': 'locator.click',
                   'params': {'selector': 'button.dots-menu'}})
    events.append({'type': 'log', 'callId': 'call@99', 'message': 'waiting for locator'})
    events.append({'type': 'log', 'callId': 'call@99', 'message': 'retrying click action'})
    events.append({'type': 'after', 'callId': 'call@99',
                   'error': {'message': 'Timeout 30000ms exceeded.\nCall log:', 'name': 'TimeoutError'}})
    events.append({'type': 'console', 'messageType': 'error', 'text': 'Failed to load resource'})
    events.append({'type': 'console', 'messageType': 'warning', 'text': 'not an error'})
    events.append({'type': 'event', 'method': 'console',
                   'params': {'type': 'error', 'text': 'Uncaught TypeError'}})
    return events


def _write_trace_zip(path: Path, events: list[dict] | None = None) -> Path:
    events = _trace_events() if events is None else events
    with zipfile.ZipFile(path, 'w') as zf:
        zf.writestr('trace.trace', '\n'.join(json.dumps(e) for e in events) + '\nnot json\n')
        zf.writestr('trace.network', '')
        zf.writestr('resources/page.jpeg', b'jpeg')
    return path


def test_digest_trace_reads_a_synthetic_zip(tmp_path):
    """The trace digest yields the URL, errors, console, and the failing action window."""
    digest = rr.digest_trace(_write_trace_zip(tmp_path / 't-trace.zip'))
    assert digest['files'] == 3
    assert digest['url'] == 'https://app.example/views'
    assert digest['errors'] == ['Timeout 30000ms exceeded.']
    assert digest['console'] == ['Failed to load resource', 'Uncaught TypeError']
    # The window ends at the failing action and holds LAST_ACTIONS before it.
    assert len(digest['actions']) == rr.LAST_ACTIONS + 1
    last = digest['actions'][-1]
    assert last['name'] == 'locator.click'
    assert last['params'] == 'selector=button.dots-menu'
    assert last['error'].startswith('Timeout 30000ms')
    assert last['log'] == ['waiting for locator', 'retrying click action']
    assert digest['actions'][0]['name'] == 'Frame.click'
    assert digest['actions'][0]['log'] == []


def test_digest_trace_without_a_failure_keeps_the_last_actions(tmp_path):
    """A trace with no failure digests to its last actions and no errors."""
    events = [e for e in _trace_events() if e.get('callId') != 'call@99']
    digest = rr.digest_trace(_write_trace_zip(tmp_path / 't-trace.zip', events))
    assert digest['errors'] == []
    assert len(digest['actions']) == rr.LAST_ACTIONS
    assert digest['actions'][-1]['params'] == 'selector=#btn-20'


def test_digest_trace_tolerates_a_bad_zip(tmp_path):
    """A corrupt or missing trace digests to an error line, not a crash."""
    bad = tmp_path / 'bad-trace.zip'
    bad.write_bytes(b'not a zip')
    digest = rr.digest_trace(bad)
    assert digest['actions'] == []
    assert len(digest['errors']) == 1
    assert digest['errors'][0].startswith('could not open trace')
    assert rr.digest_trace(tmp_path / 'missing.zip')['errors']


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

FAILED_LOG = ('self = <TestViews object>\n\n    def test_open(self):\n>       page.click()\n'
              'E   playwright._impl._errors.TimeoutError: Locator.click: Timeout 30000ms exceeded.\n'
              'E   Call log:\n'
              "E     - waiting for locator(\"button.dots-menu\")\n"
              '\ntests/ui/views/test_views_basic.py:12: TimeoutError\n'
              '--- Captured stdout setup ---\n[AUTH] Cached session is valid\n')


def _report_html(rows: dict[str, list[dict]]) -> str:
    """A pytest-html 4 shaped report: the results live in data-jsonblob."""
    blob = {'environment': {}, 'tests': rows, 'renderCollapsed': [], 'title': 'report'}
    return ('<!doctype html><html><body><div id="data-container" data-jsonblob="'
            + html.escape(json.dumps(blob), quote=True) + '"></div></body></html>')


def _row(result: str, log: str = '', duration: str = '00:00:05') -> dict:
    return {'result': result, 'duration': duration, 'log': html.escape(log), 'extras': []}


def test_load_report_results_reads_the_jsonblob(tmp_path):
    """Outcomes, durations, and logs are read from pytest-html's data-jsonblob."""
    (tmp_path / 'report.html').write_text(_report_html({
        NODEID: [_row('Failed', FAILED_LOG, '00:01:41')],
        TWIN_A: [_row('Passed')],
        TWIN_B: [_row('Rerun'), _row('Skipped')],
    }), encoding='utf-8')
    rows = rr.load_report_results(tmp_path)
    assert rows[NODEID]['outcome'] == 'failed'
    assert rows[NODEID]['duration'] == '00:01:41'
    assert 'locator("button.dots-menu")' in rows[NODEID]['log']  # unescaped twice
    assert rows[TWIN_A]['outcome'] == 'passed'
    assert rows[TWIN_B]['outcome'] == 'skipped'  # the rerun row is not a verdict


def test_load_report_results_tolerates_missing_or_odd_report(tmp_path):
    """A missing, blob-less, or broken report.html loads as no results."""
    assert rr.load_report_results(tmp_path) == {}
    (tmp_path / 'report.html').write_text('<html>no blob</html>', encoding='utf-8')
    assert rr.load_report_results(tmp_path) == {}
    (tmp_path / 'report.html').write_text('<div data-jsonblob="{broken"></div>', encoding='utf-8')
    assert rr.load_report_results(tmp_path) == {}


def test_load_results_json_accepts_both_shapes(tmp_path):
    """last_run_results.json loads from both the list shape and the dict shape."""
    (tmp_path / 'last_run_results.json').write_text(json.dumps({
        'results': [{'nodeid': NODEID, 'outcome': 'failed'},
                    {'nodeid': TWIN_A, 'outcome': 'passed'},
                    {'nodeid': '', 'outcome': 'passed'}]}), encoding='utf-8')
    assert rr.load_results_json(tmp_path) == {NODEID: 'failed', TWIN_A: 'passed'}
    (tmp_path / 'last_run_results.json').write_text(json.dumps({NODEID: 'skipped'}),
                                                    encoding='utf-8')
    assert rr.load_results_json(tmp_path) == {NODEID: 'skipped'}
    (tmp_path / 'last_run_results.json').write_text('[]', encoding='utf-8')
    assert rr.load_results_json(tmp_path) == {}


def test_load_summary_tolerates_missing_file(tmp_path):
    """A run without summary.json loads an empty summary."""
    assert rr.load_summary(tmp_path) == {}


# ---------------------------------------------------------------------------
# Collect + render on a synthetic run folder
# ---------------------------------------------------------------------------

def _make_run(tmp_path: Path, *, report: bool = True, results_json: bool = False) -> Path:
    run = tmp_path / '2026-08-24_10-00-00__tests-ui-views'
    (run / 'screenshots').mkdir(parents=True)
    (run / 'traces').mkdir()
    (run / 'summary.json').write_text(json.dumps({
        'started': '2026-08-24T10:00:00', 'duration_s': 101.8, 'exitstatus': 1,
        'counts': {'passed': 1, 'failed': 1, 'skipped': 1, 'errors': 0},
        'target': ['tests/ui/views'],
        'advisories': [{'test': TWIN_A, 'phase': 'call',
                        'line': '[VIEWS] view drag: mouse drag did not consume a tile'}],
    }), encoding='utf-8')
    if report:
        (run / 'report.html').write_text(_report_html({
            NODEID: [_row('Failed', FAILED_LOG, '00:01:41')],
            TWIN_A: [_row('Passed')],
            TWIN_B: [_row('Skipped')],
        }), encoding='utf-8')
    if results_json:
        (run / 'last_run_results.json').write_text(json.dumps({'results': [
            {'nodeid': NODEID, 'outcome': 'failed'},
            {'nodeid': TWIN_A, 'outcome': 'passed'},
            {'nodeid': TWIN_B, 'outcome': 'skipped'},
        ]}), encoding='utf-8')
    shots = run / 'screenshots'
    (shots / f'{SAFE}__FAILED.png').write_bytes(b'png')
    (shots / f'{SAFE}__FAILED__mid-drag-1.png').write_bytes(b'png')
    twin_full = f'{rr._safe_name(TWIN_A, 120)}__PASSED'
    (shots / f'{_shorten(twin_full, 30)}.png').write_bytes(b'png')
    (shots / f'{rr._safe_name(TWIN_B, 120)}__SKIPPED__mid-drag-1.png').write_bytes(b'png')
    _write_trace_zip(run / 'traces' / f'{SAFE}-trace.zip')
    (run / 'traces' / f'{SAFE}-video.webm').write_bytes(b'webm')
    return run


def test_collect_attaches_every_file_to_its_test(tmp_path):
    """Every screenshot, frame, trace, and video attaches to its test."""
    run = _make_run(tmp_path)
    tests, unattached = rr.collect_tests(run)
    assert unattached == []
    failed = tests[NODEID]
    assert failed.outcome == 'failed'
    assert failed.screenshots == [f'screenshots/{SAFE}__FAILED.png']
    assert failed.frames == [f'screenshots/{SAFE}__FAILED__mid-drag-1.png']
    assert failed.traces == [f'traces/{SAFE}-trace.zip']
    assert failed.videos == [f'traces/{SAFE}-video.webm']
    assert 'Timeout 30000ms' in failed.log
    passed = tests[TWIN_A]
    assert passed.outcome == 'passed'
    assert len(passed.screenshots) == 1
    assert '__PASSED' in passed.screenshots[0]
    assert passed.advisories == ['[VIEWS] view drag: mouse drag did not consume a tile']
    skipped = tests[TWIN_B]
    assert skipped.outcome == 'skipped'
    assert len(skipped.frames) == 1


def test_collect_falls_back_to_results_json(tmp_path):
    """Without report.html, outcomes come from last_run_results.json."""
    run = _make_run(tmp_path, report=False, results_json=True)
    tests, _ = rr.collect_tests(run)
    assert {n: t.outcome for n, t in tests.items()} == {
        NODEID: 'failed', TWIN_A: 'passed', TWIN_B: 'skipped'}
    assert tests[NODEID].log == ''


def test_collect_derives_tests_from_file_names_alone(tmp_path):
    """Neither report.html nor results JSON: the names are the witnesses."""
    run = _make_run(tmp_path, report=False)
    (run / 'summary.json').unlink()
    tests, _ = rr.collect_tests(run)
    assert tests[SAFE].outcome == 'failed'
    assert tests[SAFE].traces == [f'traces/{SAFE}-trace.zip']
    assert tests[SAFE].videos == [f'traces/{SAFE}-video.webm']
    outcomes = sorted(t.outcome for t in tests.values())
    assert outcomes == ['failed', 'passed', 'skipped']


def test_collect_keeps_the_worse_outcome_and_lists_strays(tmp_path):
    """A kept trace outranks a passed row, and unmatched files are listed."""
    run = _make_run(tmp_path)
    # report.html says passed, but a kept trace says the test failed.
    _write_trace_zip(run / 'traces' / f'{rr._safe_name(TWIN_A, 150)}-trace.zip')
    (run / 'screenshots' / 'stray.png').write_bytes(b'png')
    (run / 'traces' / 'notes.txt').write_text('x', encoding='utf-8')
    tests, unattached = rr.collect_tests(run)
    assert tests[TWIN_A].outcome == 'failed'
    assert unattached == ['screenshots/stray.png']


def test_build_run_report_orders_sections_and_links_evidence(tmp_path):
    """run.html orders failures, advisories, passed, skipped and links the evidence."""
    run = _make_run(tmp_path)
    page = rr.build_run_report(run)
    # Failures first, then advisories, passed, skipped.
    assert (page.index('id="failures"') < page.index('id="advisories"')
            < page.index('id="passed"') < page.index('id="skipped"'))
    assert 'Failures (1)' in page
    # Header facts and links, the link text capitalized like the index's.
    for fact in ('tests/ui/views', '1 min 42 s', '<a href="report.html">Raw pytest-html</a>',
                 '<a href="../index.html">Archive Index</a>', '<a href="./">Folder</a>'):
        assert fact in page
    assert 'all runs' not in page
    assert 'href="../health.html"' not in page  # no health.html beside the runs
    (tmp_path / 'health.html').write_text('x', encoding='utf-8')
    assert '<a href="../health.html">Health</a>' in rr.build_run_report(run)
    for key, value in (('passed', '1'), ('failed', '1'), ('skipped', '1'), ('errors', '0')):
        assert f'class="chip {key}">{key.capitalize()}: {value}</span>' in page
    # The failure card: message, call log, actions, console, evidence.
    card = page[page.index('<div class="card failed"'):page.index('id="advisories"')]
    assert 'Locator.click: Timeout 30000ms exceeded.' in card
    assert 'waiting for locator' in card
    for fact in ('class="failing"', 'locator.click', 'Console errors (2)', 'Uncaught TypeError'):
        assert fact in card
    assert f'src="screenshots/{SAFE}__FAILED.png"' in card
    assert f'src="screenshots/{SAFE}__FAILED__mid-drag-1.png"' in card
    assert f'<video controls preload="metadata" src="traces/{SAFE}-video.webm">' in card
    assert f'href="traces/{SAFE}-trace.zip"' in card
    assert 'python -m playwright show-trace "' in card
    assert str(run / 'traces' / f'{SAFE}-trace.zip') in card.replace('&#x27;', "'")
    assert 'Full traceback and captured output' in card
    # Advisories carry the test and the line.
    assert '[VIEWS] view drag: mouse drag did not consume a tile' in page
    # Passed and skipped are collapsed lists with thumbnails. The skipped
    # twin's mid-drag frame stays on disk and off the page: frames show
    # on failure cards only.
    assert '<summary>Passed (1)</summary>' in page
    assert '<summary>Skipped (1)</summary>' in page
    assert page.count('<img ') - page.count('class=\"brand\"') == 3
    assert f'{rr._safe_name(TWIN_B, 120)}__SKIPPED__mid-drag-1.png' not in page
    assert 'prefers-color-scheme: dark' in page
    # The verdict sits under the title; no previous run, so no change counts.
    assert '<p class="verdict">1 of 3 tests failed; 1 test skipped.</p>' in page


def test_exit_outcome_reads_as_a_word():
    """pytest's exit status reads as a word a reader without pytest knows.
    A code outside the table stays visible as 'Exit N', no status is '-'."""
    words = [(0, 'Passed'), (1, 'Failed'), (2, 'Interrupted'), (3, 'Error'), (4, 'Error'),
             (5, 'No tests'), ('1', 'Failed'), (9, 'Exit 9'), (None, '-'), ('odd', 'odd')]
    assert [rr.exit_outcome(status) for status, _ in words] == [word for _, word in words]
    assert rr.EXIT_OUTCOMES == {0: 'Passed', 1: 'Failed', 2: 'Interrupted', 3: 'Error',
                                4: 'Error', 5: 'No tests'}


def test_outcome_html_keeps_the_code_as_a_tooltip():
    """The word shows and the numeric code sits in the tooltip."""
    assert rr.outcome_html(1) == '<span title="exit code 1">Failed</span>'
    assert rr.outcome_html(0) == '<span title="exit code 0">Passed</span>'
    assert rr.outcome_html(None) == '-'


def test_run_page_says_outcome_and_notes_in_words(tmp_path):
    """The header reads Outcome, not Exit status, and the advisory section
    reads Notes with its tooltip, under the anchor the health page links."""
    page = rr.build_run_report(_make_run(tmp_path))
    assert 'Outcome: <span title="exit code 1">Failed</span>' in page
    assert 'Exit status' not in page
    assert f'<h2 id="advisories" title="{rr.NOTES_TITLE}">Notes (1)</h2>' in page
    assert 'Advisories (' not in page
    assert '<h4>Advisories</h4>' not in page
    # The filter script names what it counts, so the index can reuse it.
    assert "' tests match'" in rr.filter_script()
    assert "' runs match'" in rr.filter_script('runs')


def test_build_run_report_survives_an_empty_folder(tmp_path):
    """An empty run folder still renders a page with zero counts."""
    run = tmp_path / '2026-08-24_10-00-00__all'
    run.mkdir()
    page = rr.build_run_report(run)
    assert 'Failures (0)' in page
    assert 'No failures in this run.' in page
    assert 'Duration: -</span>' in page
    assert 'Outcome: -' in page
    assert '<summary>Passed (0)</summary>' in page
    assert 'all' in page  # the target from the folder name


def test_build_run_report_escapes_test_names(tmp_path):
    """Markup in a test name is escaped, never rendered."""
    run = _make_run(tmp_path)
    (run / 'report.html').write_text(_report_html({
        'tests/x.py::test_a[<b>bold</b>]': [_row('Failed', 'E   assert 1 == 2')]}),
        encoding='utf-8')
    page = rr.build_run_report(run)
    assert '<b>bold</b>' not in page
    assert '&lt;b&gt;bold&lt;/b&gt;' in page


def test_failure_card_without_trace_or_video_says_so(tmp_path):
    """A failure with no trace or video says so instead of linking nothing."""
    run = _make_run(tmp_path)
    (run / 'traces' / f'{SAFE}-trace.zip').unlink()
    (run / 'traces' / f'{SAFE}-video.webm').unlink()
    page = rr.build_run_report(run)
    assert 'No trace kept for this test.' in page
    assert 'No video kept for this test.' in page
    assert 'show-trace' not in page


def test_headline_takes_the_first_error_block():
    """The headline is the first E-line block of the log, or empty."""
    assert rr._headline(FAILED_LOG).splitlines()[0] == (
        'playwright._impl._errors.TimeoutError: Locator.click: Timeout 30000ms exceeded.')
    assert rr._headline('no error lines') == ''


# ---------------------------------------------------------------------------
# Titles, modules, change chips
# ---------------------------------------------------------------------------

def test_readable_title_takes_the_docstring_first_line():
    """The readable title is the docstring's first non-blank line."""
    doc = '\n    Verify the view opens.\n\n    Longer text nobody needs in a list.\n    '
    assert rr.readable_title(NODEID, doc) == 'Verify the view opens.'


@pytest.mark.parametrize(('nodeid', 'title'), [
    (NODEID, 'Open'),
    ('tests/ui/x.py::test_drag_camera_to_tile', 'Drag camera to tile'),
    ('tests/ui/x.py::TestA::test_open_my_views[chromium-site-3]', 'Open my views'),
    ('tests/ui/x.py::testSomething', 'Something'),
])
def test_readable_title_falls_back_to_the_function_name(nodeid, title):
    """Without a docstring, the title is the function name made readable."""
    assert rr.readable_title(nodeid) == title
    assert rr.readable_title(nodeid, '   \n\n') == title  # a blank docstring is no title


def test_module_of_is_the_path_under_tests():
    """The module label is the file path under tests/, slashes normalized."""
    assert rr.module_of(NODEID) == 'ui/views/test_views_basic.py'
    assert rr.module_of('tests\\ui\\x.py::test_a') == 'ui/x.py'
    assert rr.module_of('other/y.py::test_b') == 'other/y.py'
    assert rr.module_of(SAFE) == SAFE  # a file-name stand-in has no path to strip


def test_format_seconds():
    """Durations render as seconds under a minute and as min/s above."""
    assert rr.format_seconds(12.34) == '12.3 s'
    assert rr.format_seconds(0) == '0.0 s'
    assert rr.format_seconds(101.8) == '1 min 42 s'


def test_compute_deltas_labels_every_change():
    """Deltas label new failures, fixes, still-failing, and new tests only."""
    previous = {'a': 'passed', 'b': 'failed', 'c': 'failed', 'd': 'skipped',
                'e': 'passed', 'f': 'error', 'h': 'skipped'}
    current = {'a': 'failed', 'b': 'passed', 'c': 'error', 'd': 'failed',
               'e': 'passed', 'f': 'skipped', 'g': 'passed', 'h': 'passed'}
    assert rr.compute_deltas(current, previous) == {
        'a': rr.DELTA_NEW_FAILURE, 'b': rr.DELTA_FIXED, 'c': rr.DELTA_STILL_FAILING,
        'd': rr.DELTA_NEW_FAILURE, 'g': rr.DELTA_NEW_TEST}
    # The labels are the words a reader sees, not framework vocabulary.
    assert (rr.DELTA_NEW_FAILURE, rr.DELTA_FIXED, rr.DELTA_STILL_FAILING, rr.DELTA_NEW_TEST) == \
        ('New failure', 'Fixed', 'Still failing', 'New test')


def test_compute_deltas_without_a_previous_run():
    """With no previous run, or no current tests, there are no deltas."""
    assert rr.compute_deltas({'a': 'failed'}, None) == {}
    assert rr.compute_deltas({}, {'a': 'failed'}) == {}


def test_find_previous_run_wants_the_same_slug_and_an_earlier_start(tmp_path):
    """The previous run is the latest earlier folder with the same slug."""
    for name in ('2026-08-01_10-00-00__views', '2026-08-02_10-00-00__views',
                 '2026-08-03_10-00-00__views', '2026-08-02_12-00-00__media', 'notes'):
        (tmp_path / name).mkdir()
    (tmp_path / '2026-08-02_11-00-00__views').write_text('a file, not a run', encoding='utf-8')
    assert rr.find_previous_run(tmp_path / '2026-08-03_10-00-00__views') == \
        tmp_path / '2026-08-02_10-00-00__views'
    assert rr.find_previous_run(tmp_path / '2026-08-01_10-00-00__views') is None
    assert rr.find_previous_run(tmp_path / '2026-08-02_12-00-00__media') is None
    assert rr.find_previous_run(tmp_path / 'notes') is None
    assert rr.find_previous_run(tmp_path / 'missing' / '2026-08-03_10-00-00__views') is None


def test_load_previous_outcomes_prefers_the_summary_records(tmp_path):
    """Previous outcomes come from summary.json, else report.html, else nothing."""
    prev = tmp_path / '2026-08-01_10-00-00__views'
    prev.mkdir()
    (prev / 'summary.json').write_text(json.dumps({'tests': [
        {'nodeid': NODEID, 'outcome': 'failed'}, {'nodeid': TWIN_A, 'outcome': 'passed'}]}),
        encoding='utf-8')
    assert rr.load_previous_outcomes(prev) == {NODEID: 'failed', TWIN_A: 'passed'}
    # An older archive without records still answers from its report.
    (prev / 'summary.json').unlink()
    (prev / 'report.html').write_text(_report_html({TWIN_B: [_row('Passed')]}), encoding='utf-8')
    assert rr.load_previous_outcomes(prev) == {TWIN_B: 'passed'}
    assert rr.load_previous_outcomes(tmp_path / 'empty') == {}


def test_summary_tests_tolerates_odd_shapes():
    """Only dict rows with a node id survive reading the summary's tests."""
    assert rr.summary_tests({}) == []
    assert rr.summary_tests({'tests': 'nope'}) == []
    assert rr.summary_tests({'tests': [{'nodeid': 'a'}, {'x': 1}, 'str', {'nodeid': ''}]}) == \
        [{'nodeid': 'a'}]


def _summary_test_rows() -> list[dict]:
    """summary.json "tests" rows as run_archive writes them."""
    return [
        {'nodeid': NODEID, 'module': 'ui/views/test_views_basic.py',
         'title': 'Verify the view opens.', 'case_key': 'VK1', 'case_title': 'Open a view',
         'outcome': 'failed', 'duration_s': 101.2, 'phases': {'setup': 1.2, 'call': 100.0},
         'log_file': f'log/{SAFE}.log'},
        {'nodeid': TWIN_A, 'module': 'ui/c2c/test_cams.py', 'title': 'Reboot a camera',
         'case_key': None, 'case_title': None, 'outcome': 'passed', 'duration_s': 7.5,
         'phases': {'call': 7.5}, 'log_file': None},
        {'nodeid': TWIN_B, 'module': 'ui/c2c/test_cams.py', 'title': 'Reboot a camera',
         'case_key': None, 'case_title': None, 'outcome': 'skipped', 'duration_s': 0.4,
         'phases': {'setup': 0.4}, 'log_file': None},
    ]


def _add_summary_tests(run: Path, rows: list[dict], provenance: dict | None = None) -> None:
    summary = json.loads((run / 'summary.json').read_text(encoding='utf-8'))
    summary['tests'] = rows
    if provenance:
        summary['provenance'] = provenance
    (run / 'summary.json').write_text(json.dumps(summary), encoding='utf-8')


def test_build_run_report_shows_titles_facts_and_change_chips(tmp_path):
    """run.html shows titles, cases, provenance, delta chips, and the slowest list."""
    run = _make_run(tmp_path)
    _add_summary_tests(run, _summary_test_rows(), {
        'env': 'qa', 'box': 'us', 'base_url_host': 'app.example.com', 'git_commit': 'abc1234',
        'playwright': '1.62.0', 'pytest_playwright': '0.9.0',
        'browser': 'chromium', 'browser_channel': 'chrome'})
    # The previous run of this target: NODEID passed, TWIN_A failed, TWIN_B absent.
    prev = tmp_path / '2026-08-23_10-00-00__tests-ui-views'
    prev.mkdir()
    (prev / 'summary.json').write_text(json.dumps({'tests': [
        {'nodeid': NODEID, 'outcome': 'passed'}, {'nodeid': TWIN_A, 'outcome': 'failed'}]}),
        encoding='utf-8')
    (prev / 'run.html').write_text('x', encoding='utf-8')
    page = rr.build_run_report(run)

    card = page[page.index('<div class="card failed"'):page.index('id="advisories"')]
    # Title first, QA Touch case second, node id beneath; never a renamed test.
    assert (card.index('Verify the view opens.') < card.index('QA Touch VK1: Open a view')
            < card.index(f'<div class="nodeid">{NODEID}</div>'))
    assert '<span class="chip delta new-failure">New failure</span>' in card
    assert '1 min 41 s' in card
    assert f'href="log/{SAFE}.log"' in card
    # Its error was not in the previous run, and its strip reads passed then failed.
    assert '<span class="chip delta new-error">New error</span>' in card
    assert 'Passed 1/2' in card
    assert 'Flaky 50%' in card
    assert ('class="blk passed" href="../2026-08-23_10-00-00__tests-ui-views/run.html" '
            'title="Aug 23, 10:00 AM: Passed"') in card
    key = re.search(r'<div class="card failed"[^>]*data-test="([^"]*)"', page).group(1)
    assert 'verify the view opens.' in key
    assert NODEID.lower() in key
    assert 'vk1' in key
    # The passed twin was fixed; the skipped twin is new.
    assert 'chip delta fixed">Fixed<' in page
    assert 'chip delta new-test">New test<' in page
    assert '<h4 class="module">ui/c2c/test_cams.py (1)</h4>' in page
    # Header provenance, the verdict, and the plain-language delta note.
    for fact in ('Env: <code>QA</code>', 'Box: <code>us</code>', 'app.example.com', 'abc1234',
                 'Chromium / Chrome', 'Playwright 1.62.0, Pytest-Playwright 0.9.0'):
        assert fact in page
    # The box sits between Env and Host: the two boxes share a host.
    assert page.index('Env: <code>QA</code>') < page.index('Box: <code>us</code>') < page.index('app.example.com')
    assert '<p class="verdict">1 of 3 tests failed - 1 new failure, 1 fixed; 1 test skipped.</p>' in page
    assert ('Changes since the <a href="../2026-08-23_10-00-00__tests-ui-views/run.html">'
            'previous run of these tests (Aug 23, 10:00 AM)</a>. Each changed test carries a '
            'label: New failure, Fixed, Still failing, or New test.') in page
    assert 'Change chips compare' not in page
    # Slowest sits between advisories and passed, longest first.
    assert page.index('id="advisories"') < page.index('id="slowest"') < page.index('id="passed"')
    slow = page[page.index('id="slowest"'):page.index('id="passed"')]
    assert slow.index('1 min 41 s') < slow.index('7.5 s') < slow.index('0.4 s')
    # The filter box and its script ship with the page.
    assert 'id="filter"' in page
    assert "getElementById('filter')" in page


def test_build_run_report_without_a_previous_run_has_no_chips(tmp_path):
    """With no earlier run of the target, the page shows no change labels,
    says so in plain words, and every strip holds this run alone."""
    run = _make_run(tmp_path)
    _add_summary_tests(run, _summary_test_rows())
    page = rr.build_run_report(run)
    for label in ('new-failure', 'still-failing', 'fixed', 'new-test'):
        assert f'chip delta {label}' not in page
    assert 'First run of these tests - nothing to compare yet.' in page
    assert 'Passed 1/1' in page
    assert 'Passed 0/1' in page
    assert 'Flaky' not in page.split('<style>')[1].split('</style>')[1]
    assert 'New error' in page


# ---------------------------------------------------------------------------
# Verdict, delta wording, time formatting
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(('passed', 'failed', 'skipped', 'deltas', 'text'), [
    (49, 0, 0, {}, 'All 49 tests passed.'),
    (49, 0, 0, {'a': 'Fixed', 'b': 'Fixed'}, 'All 49 tests passed - 2 fixed.'),
    (48, 0, 1, {}, '48 of 49 tests passed; 1 test skipped.'),
    (45, 3, 1, {'a': 'New failure', 'b': 'New failure', 'c': 'Still failing', 'd': 'New test'},
     '3 of 49 tests failed - 2 new failures, 1 still failing; 1 test skipped.'),
    (0, 1, 0, None, '1 of 1 test failed.'),
    (1, 0, 0, {}, '1 test ran and passed.'),
    (0, 2, 2, {'a': 'Still failing', 'b': 'Still failing'},
     '2 of 4 tests failed - 2 still failing; 2 tests skipped.'),
    (0, 0, 0, {}, 'No tests ran.'),
])
def test_run_verdict_in_plain_words(passed, failed, skipped, deltas, text):
    """The verdict names exact counts and the change counts, no adjectives."""
    assert rr.run_verdict(passed, failed, skipped, deltas) == text


def test_delta_wording_with_and_without_a_previous_run():
    """The delta note names the previous run by time, or says it is the first run."""
    assert rr.delta_wording(None) == 'First run of these tests - nothing to compare yet.'
    previous = {'name': '2026-08-24_14-27-25__tests-ui-views', 'started': '2026-08-24T14:27:25'}
    assert rr.delta_wording(previous) == \
        'Changes since the previous run of these tests (Aug 24, 2:27 PM)'
    linked = rr.delta_wording(previous, lambda label: f'<a>{label}</a>')
    assert linked == 'Changes since the <a>previous run of these tests (Aug 24, 2:27 PM)</a>'


@pytest.mark.parametrize(('text', 'expected'), [
    ('2026-08-24T14:27:25', 'Aug 24, 2:27 PM'),
    ('2026-08-24_14-27-25', 'Aug 24, 2:27 PM'),
    ('2026-01-05T00:05:00', 'Jan 5, 12:05 AM'),
    ('2026-12-31T12:00', 'Dec 31, 12:00 PM'),
    ('2026-13-01T00:00:00', '2026-13-01T00:00:00'),  # no month 13: the raw text stands
    ('yesterday', 'yesterday'),
    (None, '-'),
])
def test_friendly_time(text, expected):
    """Start times read as 'Aug 24, 2:27 PM'; anything else passes through."""
    assert rr.friendly_time(text) == expected


@pytest.mark.parametrize(('text', 'with_year', 'expected'), [
    ('2026-08-24T14:27:25', False, 'Aug 24'),
    ('2026-08-24_14-27-25', True, 'Aug 24, 2026'),
    ('2026-01-05T00:05:00', True, 'Jan 5, 2026'),
    ('yesterday', True, 'yesterday'),
    (None, False, '-'),
])
def test_friendly_date(text, with_year, expected):
    """Start times read as 'Aug 24', with the year on request; anything else passes through."""
    assert rr.friendly_date(text, with_year) == expected


def test_run_started_prefers_the_summary(tmp_path):
    """The start time comes from summary.json, else from the folder stamp."""
    run = tmp_path / '2026-08-24_14-27-25__views'
    assert rr.run_started(run, {'started': '2026-08-24T14:27:30'}) == '2026-08-24T14:27:30'
    assert rr.run_started(run, {}) == '2026-08-24_14-27-25'
    assert rr.run_started(run) == '2026-08-24_14-27-25'


# ---------------------------------------------------------------------------
# Error signature and failure grouping
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(('a', 'b'), [
    ('Locator.click: Timeout 30000ms exceeded.', 'Locator.click: Timeout 15000ms exceeded.'),
    ("AssertionError: API-created group 'AutoTest Group 131103' not found for delete",
     "AssertionError: API-created group 'AutoTest Group 224455' not found for delete"),
    ('Locator expected to have count \'3\'\n    Actual value: 4',
     'Locator expected to have count \'5\'\n    Actual value: 6'),
    ('waiting for locator("button.dots-menu").first',
     'waiting for locator("button.option-menu").first'),
    ('waiting for get_by_text("AutoTest 12").first.locator("xpath=ancestor::*[1]")',
     'waiting for get_by_text("AutoTest 34").first.locator("xpath=ancestor::*[2]")'),
    ('where delete = <AdminPage object at 0x000001CE4B6A9940>.delete',
     'where delete = <AdminPage object at 0x7f3a1c00beef>.delete'),
    ('E   assert 1 == 2', 'assert 10 == 20'),
])
def test_error_signature_ignores_the_varying_parts(a, b):
    """Numbers, quoted values, hex addresses, and locator text do not split a signature."""
    assert rr.error_signature(a) == rr.error_signature(b)
    assert rr.error_signature(a)


def test_error_signature_keeps_different_errors_apart():
    """Different messages keep different signatures; an empty error has none."""
    timeout = rr.error_signature('Locator.click: Timeout 30000ms exceeded.')
    assert timeout == 'Locator.click: Timeout #ms exceeded.'
    assert timeout != rr.error_signature('Locator.fill: Timeout 30000ms exceeded.')
    assert rr.error_signature('') == ''
    assert rr.error_signature('\n  \n') == ''


def test_error_line_takes_the_first_e_line_else_the_first_line():
    """The recorded error line is the first 'E ' line, else the first line, capped."""
    assert rr.error_line(FAILED_LOG) == \
        'playwright._impl._errors.TimeoutError: Locator.click: Timeout 30000ms exceeded.'
    assert rr.error_line('\nfixture "api" not found\nmore') == 'fixture "api" not found'
    assert rr.error_line('') == ''
    assert len(rr.error_line('E   ' + 'x' * 1000)) == rr.ERROR_LINE_MAX


def _rec(nodeid: str, signature: str = '', outcome: str = 'failed') -> rr.TestRecord:
    rec = rr.TestRecord(nodeid, outcome=outcome)
    rec.signature = signature
    rec.headline = signature
    return rec


def test_group_failures_by_signature_in_first_seen_order():
    """Failures with one signature form a group where the first appeared;
    a failure without a signature stands alone."""
    a, b, c, d, e = (_rec('a', 'timeout'), _rec('b', 'count'), _rec('c', 'timeout'),
                     _rec('d', ''), _rec('e', ''))
    groups = rr.group_failures([a, b, c, d, e])
    assert [[r.nodeid for r in g] for g in groups] == [['a', 'c'], ['b'], ['d'], ['e']]
    assert rr.group_failures([]) == []


def test_signatures_seen_takes_the_newest_run():
    """A signature maps to the newest earlier run that showed it."""
    history = [
        {'name': 'r3', 'started': '2026-08-24T12:00:00', 'errors': {'t': 'Timeout 30000ms'}},
        {'name': 'r2', 'started': '2026-08-23T12:00:00', 'errors': {'t': 'Timeout 15000ms',
                                                                     'u': 'assert 1 == 2'}},
        {'name': 'r1', 'started': '2026-08-22T12:00:00', 'errors': {}},
    ]
    seen = rr.signatures_seen(history)
    assert seen == {'Timeout #ms': {'run': 'r3', 'started': '2026-08-24T12:00:00'},
                    'assert # == #': {'run': 'r2', 'started': '2026-08-23T12:00:00'}}
    assert rr.signatures_seen([]) == {}
    assert rr.signatures_seen([{'name': 'r', 'errors': {'t': ''}}]) == {}


def test_load_run_errors_prefers_the_summary_then_the_report(tmp_path):
    """Error lines come from schema-3 records, else from report.html; nothing else."""
    run = tmp_path / '2026-08-01_10-00-00__views'
    run.mkdir()
    (run / 'report.html').write_text(_report_html({
        NODEID: [_row('Failed', FAILED_LOG)], TWIN_A: [_row('Passed')]}), encoding='utf-8')
    # No summary: the report answers.
    assert rr.load_run_errors(run) == {
        NODEID: 'playwright._impl._errors.TimeoutError: Locator.click: Timeout 30000ms exceeded.'}
    # A schema-2 summary (records without 'error'): the report still answers.
    (run / 'summary.json').write_text(json.dumps({'tests': [
        {'nodeid': NODEID, 'outcome': 'failed'}]}), encoding='utf-8')
    assert NODEID in rr.load_run_errors(run)
    # A schema-3 summary: its own error lines, and no report read.
    (run / 'summary.json').write_text(json.dumps({'tests': [
        {'nodeid': NODEID, 'outcome': 'failed', 'error': 'assert 1 == 2'},
        {'nodeid': TWIN_A, 'outcome': 'passed', 'error': None}]}), encoding='utf-8')
    assert rr.load_run_errors(run) == {NODEID: 'assert 1 == 2'}
    assert rr.load_run_errors(tmp_path / 'missing') == {}


def test_failures_with_one_error_share_a_group_and_say_seen_before(tmp_path):
    """Two failures with one signature render under 'N tests share this
    error'; an error the previous run showed is 'Seen before' with its date."""
    run = _make_run(tmp_path)
    twin_log = FAILED_LOG.replace('30000ms', '15000ms')
    (run / 'report.html').write_text(_report_html({
        NODEID: [_row('Failed', FAILED_LOG, '00:01:41')],
        TWIN_A: [_row('Failed', twin_log)],
        TWIN_B: [_row('Failed', 'E   assert 1 == 2')],
    }), encoding='utf-8')
    summary = json.loads((run / 'summary.json').read_text(encoding='utf-8'))
    summary['counts'] = {'passed': 0, 'failed': 3, 'skipped': 0, 'errors': 0}
    (run / 'summary.json').write_text(json.dumps(summary), encoding='utf-8')
    prev = tmp_path / '2026-08-23_10-00-00__tests-ui-views'
    prev.mkdir()
    (prev / 'summary.json').write_text(json.dumps({'started': '2026-08-23T10:00:00', 'tests': [
        {'nodeid': TWIN_B, 'outcome': 'failed', 'error': 'assert 5 == 6'},
        {'nodeid': NODEID, 'outcome': 'passed', 'error': None}]}), encoding='utf-8')
    (prev / 'run.html').write_text('x', encoding='utf-8')
    page = rr.build_run_report(run)
    assert 'Failures (3)' in page
    group = page[page.index('<div class="group">'):page.index('</div>\n</div>') + 12]
    assert '<b>2 tests share this error</b>' in group
    assert group.count('<div class="card failed"') == 2
    assert '<span class="chip delta new-error">New error</span>' in group
    # The label sits on the group head once, not on every member card.
    assert group.count('New error') == 1
    # The assertion failure stands alone and was seen in the previous run.
    single = page[page.index('<div class="card failed" id="tests-ui-c2c-test_cams.py-TestCams-test_reboot-camera-4"'):]
    assert ('<span class="chip delta seen-before">Seen before (last on '
            '<a href="../2026-08-23_10-00-00__tests-ui-views/run.html">Aug 23, 10:00 AM</a>)'
            '</span>') in single
    # NODEID passed before (new failure), TWIN_B failed before (still
    # failing), TWIN_A was not in the previous run (a new test, not a count).
    assert '<p class="verdict">3 of 3 tests failed - 1 new failure, 1 still failing.</p>' in page


# ---------------------------------------------------------------------------
# History strip facts and flaky rate
# ---------------------------------------------------------------------------

def _results(*outcomes: str) -> list[dict]:
    """Results newest first, named r1 (newest) onward."""
    return [{'run': f'2026-08-{20 - i:02d}_10-00-00__views', 'started': f'2026-08-{20 - i:02d}T10:00:00',
             'outcome': o} for i, o in enumerate(outcomes)]


def test_history_facts_counts_passes_failures_and_the_flaky_rate():
    """Blocks read oldest first; the rate is failures over runs present."""
    facts = rr.history_facts(_results('failed', 'passed', 'skipped', 'error', 'passed'))
    assert [b['outcome'] for b in facts['blocks']] == ['passed', 'error', 'skipped', 'passed', 'failed']
    assert (facts['runs'], facts['passed'], facts['failed']) == (5, 2, 2)
    assert facts['rate'] == 0.4
    assert facts['flaky'] is True
    assert facts['last_failure']['run'] == '2026-08-20_10-00-00__views'


def test_history_facts_is_not_flaky_without_both_outcomes():
    """All passes, or all failures, is not flaky; no results is no rate."""
    passes = rr.history_facts(_results('passed', 'passed'))
    assert (passes['rate'], passes['flaky'], passes['last_failure']) == (0.0, False, None)
    fails = rr.history_facts(_results('failed', 'failed'))
    assert (fails['rate'], fails['flaky']) == (1.0, False)
    assert fails['last_failure']['run'] == '2026-08-20_10-00-00__views'
    empty = rr.history_facts([])
    assert empty == {'blocks': [], 'runs': 0, 'passed': 0, 'failed': 0, 'rate': None,
                     'flaky': False, 'last_failure': None}


def test_history_facts_keeps_the_newest_20():
    """The strip holds the newest HISTORY_RUNS results, the older ones drop off."""
    results = _results(*(['passed'] * 19 + ['failed'] * 6))
    facts = rr.history_facts(results)
    assert rr.HISTORY_RUNS == 20
    assert facts['runs'] == 20
    assert (facts['passed'], facts['failed']) == (19, 1)
    assert facts['blocks'][0]['outcome'] == 'failed'  # the oldest kept block
    assert rr.history_facts(results, limit=3)['runs'] == 3


def test_test_results_keeps_only_the_runs_that_ran_the_test():
    """A run without the test contributes no block."""
    runs = [{'name': 'r2', 'started': 's2', 'outcomes': {'a': 'failed', 'b': 'passed'}},
            {'name': 'r1', 'started': 's1', 'outcomes': {'a': 'passed'}},
            {'name': 'r0', 'outcomes': None}]
    assert rr.test_results('a', runs) == [{'run': 'r2', 'started': 's2', 'outcome': 'failed'},
                                          {'run': 'r1', 'started': 's1', 'outcome': 'passed'}]
    assert rr.test_results('b', runs) == [{'run': 'r2', 'started': 's2', 'outcome': 'passed'}]
    assert rr.test_results('c', runs) == []


def test_render_strip_links_each_block_and_labels_flaky():
    """Each block links its run with the date and outcome as tooltip;
    the text and the flaky label follow only when asked."""
    facts = rr.history_facts(_results('failed', 'passed'))
    html_ = rr.render_strip(facts, '../')
    assert html_.index('blk passed') < html_.index('blk failed')  # oldest left
    assert ('<a class="blk failed" href="../2026-08-20_10-00-00__views/run.html" '
            'title="Aug 20, 10:00 AM: Failed"></a>') in html_
    assert 'Passed 1/2' in html_
    assert '<span class="chip delta flaky">Flaky 50%</span>' in html_
    bare = rr.render_strip(facts, '', with_text=False)
    assert 'href="2026-08-20_10-00-00__views/run.html"' in bare
    assert 'Passed 1/2' not in bare
    assert rr.render_strip(rr.history_facts([])) == ''
    assert rr.render_strip({}) == ''


def test_find_earlier_runs_newest_first_and_capped(tmp_path):
    """Earlier runs of the slug come newest first, at most `limit` of them."""
    for day in (1, 2, 3, 4):
        (tmp_path / f'2026-08-{day:02d}_10-00-00__views').mkdir()
    (tmp_path / '2026-08-02_12-00-00__media').mkdir()
    current = tmp_path / '2026-08-04_10-00-00__views'
    names = [p.name for p in rr.find_earlier_runs(current, 2)]
    assert names == ['2026-08-03_10-00-00__views', '2026-08-02_10-00-00__views']
    assert rr.find_earlier_runs(current, 0) == []
    assert len(rr.find_earlier_runs(current, 20)) == 3


def test_load_history_reads_outcomes_errors_and_start_times(tmp_path):
    """History carries each earlier run's name, start, outcomes, and error lines."""
    run = _make_run(tmp_path)
    for day, error in ((22, 'assert 1 == 2'), (23, None)):
        prev = tmp_path / f'2026-08-{day}_10-00-00__tests-ui-views'
        prev.mkdir()
        (prev / 'summary.json').write_text(json.dumps({
            'started': f'2026-08-{day}T10:00:00',
            'tests': [{'nodeid': NODEID, 'outcome': 'failed' if error else 'passed',
                       'error': error}]}), encoding='utf-8')
    history = rr.load_history(run)
    assert [h['name'] for h in history] == ['2026-08-23_10-00-00__tests-ui-views',
                                            '2026-08-22_10-00-00__tests-ui-views']
    assert history[0] == {'name': '2026-08-23_10-00-00__tests-ui-views',
                          'started': '2026-08-23T10:00:00',
                          'outcomes': {NODEID: 'passed'}, 'errors': {}}
    assert history[1]['errors'] == {NODEID: 'assert 1 == 2'}
    assert rr.load_history(run, limit=1) == []


def test_build_run_report_falls_back_on_an_older_archive(tmp_path):
    """No "tests" records: titles come from the node ids, timing from
    pytest-html, and the slowest block says why it is empty."""
    run = _make_run(tmp_path)
    page = rr.build_run_report(run)
    assert '<h3 class="title">Open</h3>' in page
    assert 'No per-test timing in this run.' in page
    assert '00:01:41' in page


# ---------------------------------------------------------------------------
# Product
# ---------------------------------------------------------------------------

def test_page_names_the_product_in_title_and_header(tmp_path, monkeypatch):
    """The provenance product leads the header and prefixes the page title.
    One product: no per-test product line."""
    monkeypatch.delenv('QATOUCH_PROJECT_ID', raising=False)
    run = _make_run(tmp_path)
    rows = [dict(r, product='NexaiQ-Neurosen') for r in _summary_test_rows()]
    _add_summary_tests(run, rows, {'env': 'qa', 'product': 'NexaiQ-Neurosen'})
    page = rr.build_run_report(run)
    assert '<title>NexaiQ-Neurosen - Test Run - Aug 24, 2026, 10:00 AM</title>' in page
    header = page[page.index('<body>'):page.index('id="failures"')]
    assert '<span class="product">Product: NexaiQ-Neurosen</span>' in header
    assert header.index('Product: NexaiQ-Neurosen') < header.index('Env: <code>QA</code>')
    assert page.count('Product: ') == 1
    # The product is a word the filter box matches.
    assert 'nexaiq-neurosen' in re.search(r'data-test="([^"]*)"', page).group(1)


def test_mixed_run_names_the_product_on_every_test(tmp_path):
    """A "Mixed:" run has no single product, so each card and row shows its own."""
    run = _make_run(tmp_path)
    rows = _summary_test_rows()
    rows[0]['product'] = 'NexaiQ-Core'
    rows[1]['product'] = rows[2]['product'] = 'NexaiQ-Camera 2 Cloud'
    _add_summary_tests(run, rows, {'product': 'Mixed: NexaiQ-Camera 2 Cloud, NexaiQ-Core'})
    page = rr.build_run_report(run)
    assert '<title>Mixed: NexaiQ-Camera 2 Cloud, NexaiQ-Core - Test Run -' in page
    card = page[page.index('<div class="card failed"'):page.index('id="advisories"')]
    assert '<span class="product">Product: NexaiQ-Core</span>' in card
    rows_html = page[page.index('id="passed"'):]
    assert rows_html.count('<span class="product">Product: NexaiQ-Camera 2 Cloud</span>') == 2


def test_older_archive_derives_the_product_from_the_node_ids(tmp_path, monkeypatch):
    """No product in the summary: the node ids answer (path rule, then the
    environment, then NexaiQ-Core), and a mix of them reads as mixed."""
    monkeypatch.delenv('QATOUCH_PROJECT_ID', raising=False)
    run = _make_run(tmp_path)
    page = rr.build_run_report(run)
    # NODEID is a views test, the twins are c2c tests.
    assert '<title>Mixed: NexaiQ-Camera 2 Cloud, NexaiQ-Core - Test Run -' in page
    assert 'Product: NexaiQ-Camera 2 Cloud</span>' in page
    monkeypatch.setenv('QATOUCH_PROJECT_ID', 'P0BK')
    later = tmp_path / '2026-08-25_10-00-00__tests-ui-views'
    later.mkdir()
    (later / 'report.html').write_text(_report_html({NODEID: [_row('Passed')]}), encoding='utf-8')
    page = rr.build_run_report(later)
    assert '<title>NexaiQ-Neurosen - Test Run -' in page
    assert rr.summary_product({}) == ''
    assert rr.summary_product({'provenance': 'no'}) == ''
    assert rr.summary_product({'provenance': {'product': 'X'}}) == 'X'
    # A schema 2 or 3 summary: its records' node ids derive the label.
    monkeypatch.delenv('QATOUCH_PROJECT_ID', raising=False)
    assert rr.summary_product({'tests': [{'nodeid': NODEID}, {'nodeid': TWIN_A}]}) == \
        'Mixed: NexaiQ-Camera 2 Cloud, NexaiQ-Core'
    assert rr.summary_product({'tests': [{'nodeid': NODEID, 'product': 'Given'}]}) == 'Given'


# ---------------------------------------------------------------------------
# Links to other runs
# ---------------------------------------------------------------------------

def test_run_href_opens_the_page_the_folder_has(tmp_path):
    """run.html when present, else report.html, else the folder; run.html
    without a folder to look in."""
    for name, files in (('2026-08-01_10-00-00__a', ('run.html', 'report.html')),
                        ('2026-08-02_10-00-00__b', ('report.html',)),
                        ('2026-08-03_10-00-00__c', ())):
        (tmp_path / name).mkdir()
        for file in files:
            (tmp_path / name / file).write_text('x', encoding='utf-8')
    assert rr.run_href('2026-08-01_10-00-00__a', tmp_path, '../') == \
        '../2026-08-01_10-00-00__a/run.html'
    assert rr.run_href('2026-08-02_10-00-00__b', tmp_path, '../') == \
        '../2026-08-02_10-00-00__b/report.html'
    assert rr.run_href('2026-08-03_10-00-00__c', tmp_path) == '2026-08-03_10-00-00__c/'
    assert rr.run_href('2026-08-04_10-00-00__missing', tmp_path) == \
        '2026-08-04_10-00-00__missing/run.html'
    assert rr.run_href('2026-08-02_10-00-00__b', None, '../') == '../2026-08-02_10-00-00__b/run.html'
    assert rr.run_href('a b', tmp_path) == 'a%20b/run.html'
    # The run being built has no page on disk yet: it links run.html anyway.
    links = rr.RunLinks(tmp_path, '2026-08-03_10-00-00__c')
    assert links.href('2026-08-03_10-00-00__c', '../') == '../2026-08-03_10-00-00__c/run.html'
    assert links.href('2026-08-02_10-00-00__b', '../') == '../2026-08-02_10-00-00__b/report.html'
    assert rr.RunLinks().href('2026-08-03_10-00-00__c') == '2026-08-03_10-00-00__c/run.html'


def test_history_and_delta_links_open_what_the_older_run_has(tmp_path):
    """Strips, the seen-before note, and the previous-run note link the
    older run's report.html when it has no run.html, and its folder when
    it has neither."""
    run = _make_run(tmp_path)
    _add_summary_tests(run, _summary_test_rows())
    prev = tmp_path / '2026-08-23_10-00-00__tests-ui-views'
    prev.mkdir()
    (prev / 'summary.json').write_text(json.dumps({'started': '2026-08-23T10:00:00', 'tests': [
        {'nodeid': NODEID, 'outcome': 'failed', 'error': 'playwright._impl._errors.TimeoutError: '
                                                          'Locator.click: Timeout 5ms exceeded.'},
        {'nodeid': TWIN_A, 'outcome': 'passed'}]}), encoding='utf-8')
    (prev / 'report.html').write_text('x', encoding='utf-8')
    older = tmp_path / '2026-08-22_10-00-00__tests-ui-views'
    older.mkdir()
    (older / 'summary.json').write_text(json.dumps({'started': '2026-08-22T10:00:00', 'tests': [
        {'nodeid': NODEID, 'outcome': 'passed'}]}), encoding='utf-8')
    page = rr.build_run_report(run)
    assert '../2026-08-23_10-00-00__tests-ui-views/run.html' not in page
    assert ('<a href="../2026-08-23_10-00-00__tests-ui-views/report.html">'
            'Previous run of these tests</a>') in page
    assert ('Changes since the <a href="../2026-08-23_10-00-00__tests-ui-views/report.html">'
            'previous run of these tests (Aug 23, 10:00 AM)</a>') in page
    assert ('class="blk failed" href="../2026-08-23_10-00-00__tests-ui-views/report.html" '
            'title="Aug 23, 10:00 AM: Failed"') in page
    assert 'class="blk passed" href="../2026-08-22_10-00-00__tests-ui-views/" ' in page
    assert ('Seen before (last on <a href="../2026-08-23_10-00-00__tests-ui-views/report.html">'
            'Aug 23, 10:00 AM</a>)') in page
    # This run's own block links its run.html: the page being written.
    assert 'href="../2026-08-24_10-00-00__tests-ui-views/run.html"' in page


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_cli_writes_run_html(tmp_path, capsys):
    """The CLI writes run.html into the run folder and prints its path."""
    run = _make_run(tmp_path)
    assert rr.main([str(run)]) == 0
    assert (run / 'run.html').is_file()
    assert str(run / 'run.html') in capsys.readouterr().out


def test_cli_latest_picks_the_newest_run(tmp_path, monkeypatch):
    """'latest' on the CLI builds the report for the newest run only."""
    from soterix_qa_shared.reporting import run_archive as ra
    runs = tmp_path / 'runs'
    for name in ('2026-08-01_10-00-00__a', '2026-08-03_10-00-00__c', 'index.html-not-a-run'):
        (runs / name).mkdir(parents=True)
    monkeypatch.setattr(ra, 'RUNS_DIR', runs)
    assert rr.main(['latest']) == 0
    assert (runs / '2026-08-03_10-00-00__c' / 'run.html').is_file()
    assert not (runs / '2026-08-01_10-00-00__a' / 'run.html').exists()


def test_cli_rejects_missing_folder_and_bad_usage(tmp_path, capsys):
    """The CLI exits 1 on a missing folder and 2 on bad arguments."""
    assert rr.main([str(tmp_path / 'nope')]) == 1
    assert 'No run folder' in capsys.readouterr().err
    assert rr.main([]) == 2
    assert rr.main(['a', 'b']) == 2
    assert rr.main(['--all', 'x']) == 2


def test_cli_all_backfills_every_run_without_a_page(tmp_path, monkeypatch, capsys):
    """--all writes run.html where it is missing, leaves existing pages
    alone, tolerates a pre-schema summary and a folder that cannot be
    read, and rebuilds the index so it links the new pages."""
    from soterix_qa_shared.reporting import run_archive as ra
    runs = tmp_path / 'runs'
    monkeypatch.setattr(ra, 'RUNS_DIR', runs)
    old = runs / '2026-08-01_10-00-00__tests-ui-views'
    old.mkdir(parents=True)
    (old / 'summary.json').write_text(json.dumps({
        'schema_version': 1, 'started': '2026-08-01T10:00:00', 'exitstatus': 0,
        'counts': {'passed': 1, 'failed': 0, 'skipped': 0, 'errors': 0},
        'target': ['tests/ui/views'], 'report_file': 'report.html'}), encoding='utf-8')
    (old / 'report.html').write_text(_report_html({NODEID: [_row('Passed')]}), encoding='utf-8')
    done = runs / '2026-08-02_10-00-00__tests-ui-views'
    done.mkdir()
    (done / 'run.html').write_text('already built', encoding='utf-8')
    (runs / '2026-08-03_10-00-00__tests-ui-views').mkdir()  # bare folder, still gets a page
    (runs / 'not-a-run').mkdir()
    broken = runs / '2026-08-04_10-00-00__broken'
    broken.mkdir()
    (broken / 'run.html').mkdir()  # a folder in the page's place: the write fails

    assert rr.main(['--all']) == 1
    out = capsys.readouterr().out
    assert (old / 'run.html').is_file()
    assert '<h3 class="title">Open</h3>' not in (old / 'run.html').read_text(encoding='utf-8')
    assert 'Passed (1)' in (old / 'run.html').read_text(encoding='utf-8')
    assert (done / 'run.html').read_text(encoding='utf-8') == 'already built'
    assert (runs / '2026-08-03_10-00-00__tests-ui-views' / 'run.html').is_file()
    assert not (runs / 'not-a-run' / 'run.html').exists()
    assert 'could not build' in out
    assert '2 run.html written, 1 failed, 1 already had one' in out
    index = (runs / 'index.html').read_text(encoding='utf-8')
    assert '2026-08-01_10-00-00__tests-ui-views/run.html' in index
    # A second pass has nothing left to write.
    assert rr.backfill_run_reports(runs) == ([], [broken])


def test_friendly_stamp_reads_as_a_date_with_the_year():
    """The archive index spans months, so its Started cell carries the year;
    text that is not a stamp stands as it is."""
    assert rr.friendly_stamp('2026-08-25T10:17:14') == 'Aug 25, 2026, 10:17 AM'
    assert rr.friendly_stamp('2026-08-25_14-27-25') == 'Aug 25, 2026, 2:27 PM'
    assert rr.friendly_stamp('') == '-'
    assert rr.friendly_stamp('yesterday') == 'yesterday'


def test_format_seconds_carries_the_minute():
    """119.6 s is 2 min 0 s, never 1 min 60 s."""
    assert rr.format_seconds(119.6) == '2 min 0 s'
    assert rr.format_seconds(12.34) == '12.3 s'


def test_outcome_label_reads_as_words():
    """Outcomes render as words: Passed, Unexpected pass, Expected failure."""
    assert rr.outcome_label('passed') == 'Passed'
    assert rr.outcome_label('xpassed') == 'Unexpected pass'
    assert rr.outcome_label('xfailed') == 'Expected failure'
    assert rr.outcome_label('odd') == 'Odd'


def test_duration_text_never_raises():
    """A missing or non-numeric duration renders as '-', never an error."""
    assert rr.duration_text(101.8) == '1 min 42 s'
    assert rr.duration_text(None) == '-'
    assert rr.duration_text('soon') == '-'


def test_compact_row_shows_short_id_and_keeps_the_full_one(tmp_path):
    """A passed row: the short id as text, the full id clipped beside it
    and on hover, no module fact (the heading names it), the thumbnail
    with a readable alt and the file name on hover, no caption."""
    run = _make_run(tmp_path)
    nodeid = 'tests/ui/views/test_x.py::TestX::test_y[chromium-a "b"]'
    (run / 'screenshots').mkdir(exist_ok=True)
    shot = run / 'screenshots' / 'tests-ui-views-test_x.py-TestX-test_y-chromium__PASSED.png'
    shot.write_bytes(b'png')
    _add_summary_tests(run, [{'nodeid': nodeid, 'outcome': 'passed', 'seconds': 1.5,
                              'title': 'Verify y.', 'module': 'ui/views/test_x.py',
                              'screenshots': ['screenshots/' + shot.name]}], {'env': 'qa'})
    page = rr.build_run_report(run)
    start = page.index('<div class="row"')
    row = page[start:page.index('</details>', start)]
    full = 'tests/ui/views/test_x.py::TestX::test_y[chromium-a &quot;b&quot;]'
    assert 'TestX::test_y[chromium-a &quot;b&quot;]' in row
    assert f'<span class="sr-only"> ({full})</span>' in row
    assert f'title="{full}"' in row
    assert '<span>ui/views/test_x.py</span>' not in row
    assert 'alt="Final screenshot"' in row and '<figcaption>' not in row
    assert f'title="{shot.name}"' in row


def test_provenance_line_names_machine_account_and_the_chrome_that_ran_it():
    """The run page says which machine ran it, as whom, signed in as which
    account, on which Chrome, and whether a newer stable is out; the tenant
    host reads as Tenant, not Host, now that the machine is a fact too."""
    from soterix_qa_shared.reporting import run_report as rr
    summary = {'provenance': {'env': 'qa', 'box': 'india', 'base_url_host': 'vsaas-qa.example',
                              'host': 'spartan1', 'operator': 'soterix', 'account': 'justin',
                              'browser': 'chromium', 'browser_channel': 'chrome',
                              'browser_version': '153.0.8010.36', 'browser_latest': '153.0.8010.47',
                              'git_commit': 'b8ca3f8'}}
    html = rr._render_provenance(summary, 'NexaiQ-Core')
    for span in ('<span>Tenant: <code>vsaas-qa.example</code></span>', '<span>Machine: <code>spartan1</code></span>',
                 '<span>Operator: <code>soterix</code></span>', '<span>Account: <code>justin</code></span>',
                 '<span>Browser: <code>Chromium / Chrome 153.0.8010.36 (stable 153.0.8010.47 is out)</code></span>'):
        assert span in html, span
    assert 'Host:' not in html
    current = {'provenance': {'browser_channel': 'chrome', 'browser_version': '1.0', 'browser_latest': '1.0'}}
    assert '<span>Browser: <code>Chrome 1.0</code></span>' in rr._render_provenance(current, '')
    assert 'Machine' not in rr._render_provenance({'provenance': {}}, '')


def test_a_recorded_outcome_is_not_overridden_by_a_leftover_trace(tmp_path):
    """A test that passed on its rerun keeps its first attempt's trace; the page
    listed it under Failures while the summary said passed (two real runs,
    evidence audit 09/18/2026). The summary's word now stands."""
    import json
    run = tmp_path / '2026-09-18_10-00-00__tests-ui'
    (run / 'traces').mkdir(parents=True)
    nodeid = 'tests/ui/x/test_x.py::test_flaps[chromium]'
    (run / 'summary.json').write_text(json.dumps({
        'schema_version': 5, 'counts': {'passed': 1, 'failed': 0, 'skipped': 0, 'errors': 0},
        'tests': [{'nodeid': nodeid, 'outcome': 'passed', 'reruns': 1, 'duration_s': 1.0}]}), encoding='utf-8')
    (run / 'traces' / 'tests-ui-x-test_x.py-test_flaps-chromium-trace.zip').write_bytes(b'x')
    tests, _ = rr.collect_tests(run)
    assert tests[nodeid].outcome == 'passed'
    assert tests[nodeid].reruns == 1
    assert tests[nodeid].traces  # the trace is still attached as evidence
    assert 'passed on a rerun' in rr.run_verdict(1, 0, 0, reruns=1)


def test_the_page_opens_with_what_to_read_first():
    """One cause, one skip reason and the unrun count lead the page; nothing when none applies."""
    html = rr._read_first_html({'one_cause': {'count': 187, 'of': 190, 'line': 'BrowserType.launch: not found', 'first': 't::a'},
                                'one_skip_reason': {'count': 49, 'reason': 'cannot decode H.265'},
                                'not_run_count': 13, 'collected': 15})
    assert 'One cause, 187 of 190 failures: BrowserType.launch: not found' in html
    assert '49 tests skipped for one reason: cannot decode H.265' in html
    assert '13 of 15 collected tests did not run' in html
    assert rr._read_first_html({}) == ''

