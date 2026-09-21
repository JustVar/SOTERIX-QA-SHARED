"""Unit tests for soterix_qa_shared.reporting.run_page.

The pure parts (the UTC stamp, the <time> element, the parameter of a node id, the
second axis, the line that several errors share, the tab of an outcome), then the
page on a small fake run folder with records built by hand, then the page's script
in a real Chrome. The browser pins skip on a machine with no Playwright or no Chrome.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from soterix_qa_shared.reporting import run_page as rp
from soterix_qa_shared.reporting import run_report as rr


@pytest.fixture(autouse=True)
def _no_axis_provider():
    """The provider is module state: a test that sets one must not name the next test's columns."""
    rp.configure_axis(None)
    yield
    rp.configure_axis(None)


CAMERAS = ('dc-5v-1-q300c5b41', 'dc-5v-1-q300c5b42', 'bc-5v-1-q300c5b43')
HOSTILE = '<script>alert({})</script>'


def _rec(nodeid: str, outcome: str = 'passed', **facts) -> rr.TestRecord:
    return rr.TestRecord(nodeid, outcome=outcome, **facts)


def _grid(functions=('test_a', 'test_b', 'test_c'), cameras=CAMERAS) -> list[rr.TestRecord]:
    """Each function on each camera, as the Camera 2 Cloud suite runs."""
    return [_rec(f'tests/ui/c2c/test_c.py::{fn}[{cam}-chromium]') for fn in functions for cam in cameras]


def _run_dir(tmp_path: Path) -> Path:
    run = tmp_path / '2026-09-21_10-00-00__tests-ui'
    run.mkdir(parents=True)
    return run


def _render(tmp_path: Path, records: list[rr.TestRecord], summary: dict | None = None, **kwargs) -> str:
    return rp.render_run_page(_run_dir(tmp_path), summary or {}, {r.nodeid: r for r in records}, [], **kwargs)


def _named_template(page: str, name: str) -> str:
    found = re.search(rf'<template id="{re.escape(name)}">(.*?)</template>', page, flags=re.DOTALL)
    assert found, f'no template {name}'
    return found.group(1)


def _template_of(page: str, safe: str) -> str:
    """The pane content of 1 test, found the way the script finds it: by the row's data-id."""
    row = re.search(rf'<div class="row"[^>]* id="{re.escape(safe)}" data-id="(\d+)"', page)
    assert row, f'no row with id {safe}'
    return _named_template(page, f't{row.group(1)}')


def _tab(template: str, index: int) -> str:
    found = re.search(rf'<section data-tab-body="{index}">(.*?)</section>', template, flags=re.DOTALL)
    assert found, f'no tab section {index}'
    return found.group(1)


def _outside_templates(page: str) -> str:
    return re.sub(r'<template.*?</template>', '', page, flags=re.DOTALL)


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(('naive', 'offset'), [
    ('2026-09-21T05:25:54', '-05:00'),
    ('2026-09-21T15:55:54', '+05:30'),
    ('2026-09-21T10:25:54', '+0000'),
    ('2026-09-21 10:25:54', '+00:00'),
    ('2026-09-21T12:25:54+02:00', None),
])
def test_utc_iso_converts_with_the_recorded_offset(naive, offset):
    """The run's own time and its recorded offset give the UTC stamp; a time with its own zone needs none."""
    assert rp.utc_iso(naive, offset) == '2026-09-21T10:25:54Z'


def test_utc_iso_without_an_offset_uses_this_machines():
    """An older run recorded no offset: the stamp is still a UTC time, by the offset of the machine that builds the page."""
    assert re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', rp.utc_iso('2026-09-21T10:25:54'))
    assert re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', rp.utc_iso('2026-09-21T10:25:54', 'odd'))


@pytest.mark.parametrize('text', ['yesterday', '', None, '2026-13-01T00:00:00'])
def test_utc_iso_of_text_that_is_no_time_is_empty(text):
    """Text that is no time gives '', never an error."""
    assert rp.utc_iso(text, '-05:00') == ''


def test_time_html_carries_the_utc_stamp_only_when_the_time_parses():
    """A time that parses gets data-utc for the script; text that does not shows as it is, escaped."""
    assert rp.time_html('2026-09-21T05:25:54', '-05:00', 'Sep 21, 5:25 AM') == \
        '<time data-utc="2026-09-21T10:25:54Z">Sep 21, 5:25 AM</time>'
    assert rp.time_html('yesterday', '-05:00', 'yesterday') == '<time>yesterday</time>'
    assert rp.time_html(None, None, '<b>-</b>') == '<time>&lt;b&gt;-&lt;/b&gt;</time>'


def test_time_html_is_short_only_on_request():
    """data-short (no year, no zone name) is there only with short=True, and only on a time that parses."""
    assert rp.time_html('2026-09-21T05:25:54', '-05:00', 'x', short=True) == \
        '<time data-utc="2026-09-21T10:25:54Z" data-short>x</time>'
    assert 'data-short' not in rp.time_html('2026-09-21T05:25:54', '-05:00', 'x')
    assert rp.time_html('yesterday', None, 'x', short=True) == '<time>x</time>'


# ---------------------------------------------------------------------------
# The parameter of a node id
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(('nodeid', 'param'), [
    ('tests/ui/c2c/test_c.py::test_x[dc-5v-1-q300c5b44-chromium]', 'dc-5v-1-q300c5b44'),
    ('tests/ui/c2c/test_c.py::test_x[chromium]', ''),
    ('tests/ui/events/test_e.py::test_x[chromium-7 days]', '7 days'),
    ('tests/ui/c2c/test_c.py::test_x[dc-5v-1-q300c5b44-chromium]@account_c2c', 'dc-5v-1-q300c5b44'),
    ('tests/ui/c2c/test_c.py::test_x[Chromium-cam-1-firefox]', 'cam-1'),
    ('tests/ui/c2c/test_c.py::test_x', ''),
    ('tests/ui/c2c/test_c.py::test_x@account_c2c', ''),
])
def test_param_of_is_the_parameter_without_the_browser(nodeid, param):
    """The browser at either end and the xdist '@group' are not part of a test's parameter."""
    assert rp.param_of(nodeid) == param


def test_function_of_is_the_node_id_without_its_parameters():
    """The tests of 1 function share 1 row of the map: the node id up to its last bracket, with no '@group'."""
    assert rp.function_of('tests/a.py::TestA::test_x[cam-1-chromium]') == 'tests/a.py::TestA::test_x'
    assert rp.function_of('tests/a.py::TestA::test_x[cam-1-chromium]@account_c2c') == 'tests/a.py::TestA::test_x'
    assert rp.function_of('tests/a.py::test_x') == 'tests/a.py::test_x'
    assert rp.function_of('tests/a.py::test_x@account_c2c') == 'tests/a.py::test_x'


# ---------------------------------------------------------------------------
# The second axis
# ---------------------------------------------------------------------------

def test_find_axis_is_none_when_the_only_parameter_is_the_browser():
    """A run whose tests differ by '[chromium]' alone has no second axis."""
    records = [_rec(f'tests/ui/views/test_v.py::test_{name}[chromium]') for name in ('a', 'b', 'c')]
    assert rp.find_axis(records, {}) is None
    assert rp.find_axis([], {}) is None


def test_find_axis_is_none_when_fewer_than_half_of_the_functions_have_two_parameters():
    """1 parametrized test in a suite of plain ones does not make the run a grid."""
    records = _grid(('test_a',), CAMERAS[:2]) + _grid(('test_b', 'test_c', 'test_d'), CAMERAS[:1])
    assert rp.find_axis(records, {}) is None
    # 2 of 4 functions is half: the run has an axis.
    assert rp.find_axis(_grid(('test_a', 'test_b'), CAMERAS[:2]) + _grid(('test_c', 'test_d'), CAMERAS[:1]), {})
    # The functions with no parameter count too: 2 tests with a date range in a suite of 100 are no axis of the run.
    ranged = [_rec(f'tests/ui/events/test_e.py::test_{fn}[chromium-{days} days]') for fn in ('filter', 'export') for days in (7, 30)]
    plain = [_rec(f'tests/ui/views/test_v.py::test_{n}[chromium]') for n in range(100)]
    assert rp.find_axis(ranged, {}) is not None
    assert rp.find_axis(plain + ranged, {}) is None


def test_find_axis_of_a_grid_names_columns_from_the_parameter_text():
    """3 tests on 3 cameras: 3 columns and 3 rows. With no names given, the group is the parameter
    without its last part, the label is the end of the last part, and the noun is 'variant'."""
    records = _grid()
    axis = rp.find_axis(records, {})
    assert axis['noun'] == 'variant'
    assert [(c['param'], c['group'], c['label'], c['title']) for c in axis['columns']] == [
        ('bc-5v-1-q300c5b43', 'BC-5V-1', '5b43', 'bc-5v-1-q300c5b43'),
        ('dc-5v-1-q300c5b41', 'DC-5V-1', '5b41', 'dc-5v-1-q300c5b41'),
        ('dc-5v-1-q300c5b42', 'DC-5V-1', '5b42', 'dc-5v-1-q300c5b42')]
    assert [(module, title) for module, title, _cells in axis['rows']] == [
        ('ui/c2c/test_c.py', 'A'), ('ui/c2c/test_c.py', 'B'), ('ui/c2c/test_c.py', 'C')]
    for _module, _title, cells in axis['rows']:
        assert set(cells) == set(CAMERAS)
    assert axis['rows'][0][2][CAMERAS[0]] is records[0]
    assert rp.column_name(axis, records[0].nodeid) == 'DC-5V-1 5b41'
    assert rp.column_name(None, records[0].nodeid) == CAMERAS[0]


def test_find_axis_takes_the_names_that_the_run_recorded_first():
    """summary['axis'] wins over the consumer's provider: the run knew its cameras when it ran."""
    rp.configure_axis(lambda params: {'noun': 'device', 'columns': {}})
    summary = {'axis': {'noun': 'camera', 'columns': {
        CAMERAS[0]: {'group': 'DC-5V-1', 'label': '.129', 'title': 'DC-5V-1 at 10.2.0.129'}}}}
    axis = rp.find_axis(_grid(), summary)
    assert axis['noun'] == 'camera'
    named = next(c for c in axis['columns'] if c['param'] == CAMERAS[0])
    assert (named['group'], named['label'], named['title']) == ('DC-5V-1', '.129', 'DC-5V-1 at 10.2.0.129')
    # A camera that the run did not name keeps the names from its parameter text.
    other = next(c for c in axis['columns'] if c['param'] == CAMERAS[1])
    assert (other['group'], other['label']) == ('DC-5V-1', '5b42')


def test_find_axis_asks_the_configured_provider_when_the_run_recorded_no_names():
    """configure_axis(provider): the provider gets the run's parameters and names the columns."""
    asked = []

    def provider(params):
        asked.append(set(params))
        return {'noun': 'camera', 'columns': {p: {'group': 'Lab', 'label': p[-2:]} for p in params}}

    rp.configure_axis(provider)
    axis = rp.find_axis(_grid(), {})
    assert asked == [set(CAMERAS)]
    assert axis['noun'] == 'camera'
    assert [(c['group'], c['label']) for c in axis['columns']] == [('Lab', '41'), ('Lab', '42'), ('Lab', '43')]


@pytest.mark.parametrize('answer', ['raises', None, 'not a mapping'])
def test_find_axis_falls_back_to_the_parameter_text_when_the_provider_fails(answer):
    """A provider that raises, or that does not know the run, costs the names and never the map."""
    def provider(params):
        if answer == 'raises':
            raise OSError('the camera registry is gone')
        return answer

    rp.configure_axis(provider)
    axis = rp.find_axis(_grid(), {})
    assert axis['noun'] == 'variant'
    assert [c['label'] for c in axis['columns']] == ['5b43', '5b41', '5b42']


def test_find_axis_is_none_past_the_widest_map():
    """More parameters than MAP_MAX_COLUMNS: no map, the rows alone show the run."""
    cameras = [f'cam-{n}' for n in range(rp.MAP_MAX_COLUMNS + 1)]
    assert rp.find_axis(_grid(('test_a', 'test_b'), cameras), {}) is None
    assert rp.find_axis(_grid(('test_a', 'test_b'), cameras[:-1]), {}) is not None


# ---------------------------------------------------------------------------
# Causes and tabs
# ---------------------------------------------------------------------------

def test_shared_line_puts_dots_where_the_errors_differ():
    """The words that the errors share stay; a word that differs reads '...', and 2 in a row read as 1."""
    assert rp.shared_line(['Camera 10.2.0.129 reports', 'Camera 10.2.0.171 reports']) == 'Camera ... reports'
    assert rp.shared_line(['Camera A on port 1 fails', 'Camera B on port 2 fails']) == 'Camera ... on port ... fails'
    assert rp.shared_line(['Camera 1 2 fails', 'Camera 3 4 fails']) == 'Camera ... fails'
    assert rp.shared_line(['same words', 'same words']) == 'same words'


def test_shared_line_of_lines_that_do_not_align_is_the_first_line():
    """Errors with different word counts cannot be compared word by word; no error gives ''."""
    assert rp.shared_line(['Timeout 30000ms exceeded.', 'Timeout exceeded.']) == 'Timeout 30000ms exceeded.'
    assert rp.shared_line(['', 'only this one']) == 'only this one'
    assert rp.shared_line([]) == ''
    assert rp.shared_line(['', '']) == ''


@pytest.mark.parametrize(('outcome', 'tab'), [
    ('failed', 'fail'), ('error', 'fail'), ('skipped', 'skip'), ('xfailed', 'skip'),
    ('passed', 'pass'), ('xpassed', 'pass'), ('unknown', 'unkn'), ('', 'unkn'), ('odd', 'unkn'),
])
def test_bucket_is_the_tab_of_the_outcome(outcome, tab):
    """Each outcome is under 1 of the tabs Failed, Skipped, Passed or Unknown."""
    assert rp.bucket(outcome) == tab


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

def test_a_green_run_says_all_passed_and_opens_on_all_tests(tmp_path):
    """No failure: the verdict is 'All N passed.', the page opens on the All tab, and it has no causes."""
    records = [_rec(f'tests/ui/views/test_v.py::test_{name}[chromium]') for name in ('a', 'b', 'c')]
    page = _render(tmp_path, records, {'exitstatus': 0})
    assert '<p class="verdict"><em class="tone-pass">All 3 passed.</em></p>' in page
    assert '<body data-first="all"' in page
    assert 'id="causes"' not in page
    assert 'id="map"' not in page   # and '[chromium]' alone is no second axis
    assert '<button type="button" data-f="all" class=on>All<b>3</b></button>' in page
    assert len(re.findall(r'<div class="row"[^>]* id="', page)) == 3
    assert len(re.findall(r'<template id="t\d+">', page)) == 3


def test_a_run_with_skips_counts_them_in_the_verdict(tmp_path):
    """Passed and skipped, no failure: 'N of M passed.' with the skipped count under it."""
    records = [_rec('tests/a.py::test_a[chromium]'), _rec('tests/a.py::test_b[chromium]', 'skipped')]
    page = _render(tmp_path, records)
    assert '<p class="verdict"><em class="tone-pass">1 of 2 passed.</em></p>' in page
    assert '<p class="sub">1 skipped.</p>' in page
    assert '<body data-first="all"' in page


def test_failures_that_share_an_error_are_one_cause_with_their_count(tmp_path):
    """2 failures with 1 signature are 1 <details class="cause">: the count, the words they share, and 1 item for each."""
    records = _grid(('test_a', 'test_b'), CAMERAS[:2])
    for rec, address in zip(records[:2], ('10.2.0.129', '10.2.0.171')):
        rec.outcome, rec.signature, rec.headline = 'failed', 'Camera # reports no stream', f'Camera {address} reports no stream'
    page = _render(tmp_path, records)
    assert '<body data-first="fail"' in page
    assert '<em class="tone-fail">2 of 4 failed.</em> 1 cause.' in page
    causes = re.findall(r'<details class="cause">.*?</details>', page, flags=re.DOTALL)
    assert len(causes) == 1
    assert '<summary><b>2</b><span class="msg">Camera ... reports no stream</span>' in causes[0]
    assert causes[0].count('<li ') == 2
    # The run is a grid, so the cause counts the columns it is on, by the map's noun, and names them.
    assert '<span class="where">2 variants<br>' in causes[0]
    assert '<p class="cols">DC-5V-1 5b41, DC-5V-1 5b42</p>' in causes[0]
    assert page.index('id="causes"') < page.index('id="map"') < page.index('id="list"')
    assert '<h2>Map: 2 tests on 2 variants</h2>' in page


def test_tests_that_did_not_run_are_said_under_the_verdict(tmp_path):
    """A run that died early: the page says how many collected tests did not run, so the counts are not read as the whole suite."""
    page = _render(tmp_path, [_rec('tests/a.py::test_a[chromium]')], {'not_run_count': 13, 'collected': 15})
    assert '13 of 15 collected tests did not run. The counts cover only what ran.' in _outside_templates(page)
    assert 'did not run' not in _render(tmp_path / 'other', [_rec('tests/a.py::test_a[chromium]')], {'collected': 15})


def test_an_interrupted_run_says_so_beside_the_verdict(tmp_path):
    """Exit status 2: the counts alone do not say that the run was cut, so an Outcome fact leads the facts. A failed run (1) needs none."""
    records = [_rec('tests/a.py::test_a[chromium]')]
    word = rr.EXIT_OUTCOMES[2]
    assert word == 'Interrupted'
    cut = _render(tmp_path, records, {'exitstatus': 2})
    assert f'<div class="facts"><div><span>Outcome</span><b class="tone-fail">{word}</b></div>' in cut
    assert f'<span>Outcome</span><span>{word}</span>' in _named_template(cut, 't-run')
    failed = _render(tmp_path / 'other', records, {'exitstatus': 1})
    assert '<span>Outcome</span>' not in _outside_templates(failed)
    assert '<span>Outcome</span><span>Failed</span>' in _named_template(failed, 't-run')


def test_text_from_the_run_is_escaped_everywhere(tmp_path):
    """A title, an error, a skip reason, a note or a name with markup in it shows as text. The only
    <script> of the page is its own."""
    failed = _rec(f'tests/ui/c2c/test_c.py::test_a[{CAMERAS[0]}-chromium]', 'failed',
                  title=HOSTILE.format(1), headline=HOSTILE.format(2), signature='sig', error=HOSTILE.format(3),
                  log=HOSTILE.format(4), advisories=[HOSTILE.format(5)], case_key='K1', case_title=HOSTILE.format(6),
                  module=HOSTILE.format(7), product=HOSTILE.format(8), delta=HOSTILE.format(9),
                  seen={'run': '2026-09-20_10-00-00__<script>alert(10)</script>', 'started': HOSTILE.format(11)})
    skipped = _rec(f'tests/ui/c2c/test_c.py::test_a[{CAMERAS[1]}-chromium]', 'skipped', reason=HOSTILE.format(12))
    odd = _rec('tests/ui/c2c/test_c.py::test_b[<script>alert(13)</script>-chromium]', 'passed', duration=HOSTILE.format(14))
    others = _grid(('test_b',), CAMERAS[:2])
    summary = {
        'started': HOSTILE.format(15), 'ended': HOSTILE.format(16), 'exitstatus': HOSTILE.format(17),
        'target': [HOSTILE.format(18)], 'collected': HOSTILE.format(19), 'not_run_count': 2,
        'one_skip_reason': {'count': 1, 'reason': HOSTILE.format(20)},
        'advisories': [{'test': HOSTILE.format(21), 'line': HOSTILE.format(22)}],
        'provenance': {key: HOSTILE.format(23) for key in (
            'env', 'box', 'base_url_host', 'host', 'operator', 'account', 'git_commit', 'browser',
            'browser_channel', 'browser_version', 'playwright', 'pytest_playwright')} | {'browser_latest': HOSTILE.format(24)},
        'axis': {'noun': HOSTILE.format(25), 'columns': {
            CAMERAS[0]: {'group': HOSTILE.format(26), 'label': HOSTILE.format(27), 'title': HOSTILE.format(28)}}},
    }
    page = rp.render_run_page(_run_dir(tmp_path), summary, {r.nodeid: r for r in (failed, skipped, odd, *others)},
                              [HOSTILE.format(29)], previous={'name': HOSTILE.format(30), 'started': HOSTILE.format(31)},
                              product='Mixed: ' + HOSTILE.format(32))
    assert 'id="map"' in page   # the hostile axis names are on the page
    own = f'<script>{rp.RUN_PAGE_JS}</script>'
    assert page.count(own) == 1
    rest = page.replace(own, '')
    assert '<script>alert' not in rest
    assert '<script' not in rest
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in rest   # the title is there, as text
    assert '&lt;script&gt;alert(12)&lt;/script&gt;' in rest   # and the skip reason


def test_a_skipped_tests_pane_says_why(tmp_path):
    """The Result tab of a skipped test shows its reason; a test with none says its outcome and no error."""
    why = _rec('tests/a.py::test_a[chromium]', 'skipped', reason='The box cannot decode H.265')
    plain = _rec('tests/a.py::test_b[chromium]', 'skipped')
    page = _render(tmp_path, [why, plain])
    pane = _template_of(page, why.safe)
    assert re.findall(r'data-tab="\d">([^<]*)<', pane) == ['Result', 'Evidence', 'History', 'Identity']
    assert '<span class="lab">Skipped because</span><pre>The box cannot decode H.265</pre>' in _tab(pane, 0)
    assert '<p class="sub">Skipped. No error.</p>' in _tab(_template_of(page, plain.safe), 0)


def test_a_test_that_passed_on_a_rerun_says_so(tmp_path):
    """The verdict counts the tests that passed on a rerun, and the test's Result tab says how many reruns."""
    rec = _rec('tests/a.py::test_a[chromium]', 'passed', reruns=2)
    page = _render(tmp_path, [rec])
    assert '1 test passed on a rerun.' in _outside_templates(page)
    assert 'Passed on a rerun (2).' in _tab(_template_of(page, rec.safe), 0)


def test_no_picture_loads_before_a_test_is_opened(tmp_path):
    """Each screenshot and video is in a <template>: the browser loads none until the script copies 1 test into the pane."""
    records = [_rec(f'tests/a.py::test_{n}[chromium]', 'failed' if n == 0 else 'passed', digest={},
                    screenshots=[f'screenshots/s{n}__PASSED.png'], frames=[f'screenshots/s{n}__FAILED__mid-drag-1.png'],
                    videos=[f'traces/s{n}-video.webm']) for n in range(3)]
    page = _render(tmp_path, records)
    outside = _outside_templates(page)
    assert outside.count('<img ') == outside.count('class="brand"')
    assert '<video' not in outside
    # 3 final screenshots, and the mid-drag frame of the failure alone.
    assert page.count('<img ') - page.count('class="brand"') == 4
    assert page.count('<video controls preload="none"') == 3


def test_run_details_link_only_the_files_that_are_there(tmp_path):
    """The Files group links the report, the log, the notes file and the health page when each is on disk; the
    archive index and the folder always. Files that matched no test are listed, their names escaped."""
    run = _run_dir(tmp_path)
    records = {r.nodeid: r for r in [_rec('tests/a.py::test_a[chromium]')]}
    bare = _named_template(rp.render_run_page(run, {}, records, []), 't-run')
    for absent in ('report.html', 'log/run.log', 'advisories.txt', '../health.html', 'Files not matched'):
        assert absent not in bare
    assert '<a href="../index.html">Archive index</a>' in bare
    assert '<a href="./">Files of this run</a>' in bare
    (run / 'report.html').write_text('x', encoding='utf-8')
    (run / 'log').mkdir()
    (run / 'log' / 'run.log').write_text('x', encoding='utf-8')
    (run / 'advisories.txt').write_text('x', encoding='utf-8')
    (tmp_path / 'health.html').write_text('x', encoding='utf-8')
    full = _named_template(rp.render_run_page(run, {}, records, ['screenshots/stray & co.png']), 't-run')
    for link in ('<a href="report.html">Raw pytest-html</a>', '<a href="log/run.log">run.log</a>',
                 '<a href="advisories.txt">advisories.txt</a>', '<a href="../health.html">Health page</a>'):
        assert link in full
    assert 'Files not matched to a test (1)' in full
    assert '<a href="screenshots/stray%20%26%20co.png">screenshots/stray &amp; co.png</a>' in full


def test_an_unknown_outcome_gets_its_own_tab(tmp_path):
    """A test with no recorded outcome is under an Unknown tab, which shows only when there is one."""
    page = _render(tmp_path, [_rec('tests/a.py::test_a[chromium]'), _rec('tests/a.py::test_b[chromium]', 'unknown')])
    assert '<button type="button" data-f="unkn">Unknown<b>1</b></button>' in page
    assert 'data-o="unkn"' in page
    assert 'data-f="unkn"' not in _render(tmp_path / 'other', [_rec('tests/a.py::test_a[chromium]')])


# ---------------------------------------------------------------------------
# The page's script, in Chrome
# ---------------------------------------------------------------------------

def _browser_run(tmp_path: Path) -> tuple[Path, list[rr.TestRecord]]:
    """3 tests on 3 cameras: 'Stream opens' fails on 2 cameras with 1 error, 'Snapshot saves' is
    skipped on 1, 'Reboot works' passes on each. Written to run.html in a run folder."""
    titles = {'test_a': 'Stream opens', 'test_b': 'Snapshot saves', 'test_c': 'Reboot works'}
    records = _grid()
    for rec in records:
        rec.title = titles[rec.nodeid.split('::')[1].split('[')[0]]
    for rec, address in zip(records[:2], ('10.2.0.129', '10.2.0.171')):
        rec.outcome, rec.signature, rec.headline = 'failed', 'Camera # reports no stream', f'Camera {address} reports no stream'
    records[4].outcome, records[4].reason = 'skipped', 'The camera has no SD card'
    summary = {'started': '2026-09-21T05:25:54', 'utc_offset': '-05:00', 'exitstatus': 1,
               'advisories': [{'test': records[0].nodeid, 'line': '[C2C] the stream took 9 s to open'}]}
    run = _run_dir(tmp_path)
    (run / 'run.html').write_text(rp.render_run_page(run, summary, {r.nodeid: r for r in records}, []), encoding='utf-8')
    return run / 'run.html', records


VISIBLE_ROWS = "() => [...document.querySelectorAll('#list .row')].filter(r => !r.classList.contains('off')).map(r => r.dataset.o)"
VISIBLE_MAP_ROWS = "() => [...document.querySelectorAll('#map tbody tr:not(.mod)')].filter(r => !r.classList.contains('off')).length"
VISIBLE_SECTIONS = "() => [...document.querySelectorAll('#pane section')].filter(s => getComputedStyle(s).display !== 'none').map(s => s.dataset.tabBody)"


def test_the_page_works_in_a_browser(tmp_path):
    """In a browser: the pane rests on Run details; a row opens its test under 4 tabs and Escape closes it; a tab
    of the list narrows the rows, the map and the causes; the filter box narrows the rows; the clock button
    switches Central time and India time; a link to '#<row id>' opens that test. No script error."""
    sync_api = pytest.importorskip('playwright.sync_api')
    path, records = _browser_run(tmp_path)
    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(channel='chrome')
        except sync_api.Error:
            pytest.skip('no Chrome on this machine')
        errors: list[str] = []
        page = browser.new_page()
        page.on('pageerror', lambda err: errors.append(str(err)))
        page.goto(path.as_uri())
        # At rest: the run's details, and the Failed tab with its 2 rows.
        assert page.inner_text('#pane h3') == 'Run details'
        assert page.evaluate(VISIBLE_ROWS) == ['fail', 'fail']
        assert page.inner_text('#list-h').lower() == 'tests: 2'
        # A failed row opens its test under 4 tabs, the first one shown.
        page.click('#list .row[data-o="fail"] >> nth=0')
        assert page.inner_text('#pane h3') == 'Stream opens'
        assert page.locator('#pane [data-tab]').count() == 4
        assert page.evaluate(VISIBLE_SECTIONS) == ['0']
        assert 'reports no stream' in page.inner_text('#pane section.on')
        page.click('#pane [data-tab="1"]')
        assert page.evaluate(VISIBLE_SECTIONS) == ['1']
        page.keyboard.press('Escape')
        assert page.inner_text('#pane h3') == 'Run details'
        # The map follows the tab: on Failed it shows the 1 test that failed, until asked for all.
        assert page.evaluate(VISIBLE_MAP_ROWS) == 1
        assert page.inner_text('#maptoggle').lower() == 'show all 3'
        page.click('#maptoggle')
        assert page.evaluate(VISIBLE_MAP_ROWS) == 3
        assert page.inner_text('#maptoggle').lower() == 'show only the tests that failed'
        assert page.inner_text('#mapcount') == 'Showing all 3 tests.'
        # The Skipped tab: only skipped rows, and the causes are about failures, so they hide.
        assert page.is_visible('#causes')
        page.click('nav button[data-f="skip"]')
        assert page.is_hidden('#causes')
        assert page.evaluate(VISIBLE_ROWS) == ['skip']
        assert page.evaluate(VISIBLE_MAP_ROWS) == 1
        # The filter box narrows the rows of the tab that is open.
        page.click('nav button[data-f="all"]')
        assert len(page.evaluate(VISIBLE_ROWS)) == 9
        page.fill('#filter', 'Q300C5B42')
        assert len(page.evaluate(VISIBLE_ROWS)) == 3
        page.fill('#filter', '')
        assert len(page.evaluate(VISIBLE_ROWS)) == 9
        # The clock: 12 hour Central time first, then 24 hour India time, each with its zone named.
        assert re.search(r'^Sep 21, 2026, 5:25 AM (CDT|CST)$', page.text_content('h1 time'))
        assert page.inner_text('#clock').lower() == '12 h'
        page.click('#clock')
        assert page.text_content('h1 time') == 'Sep 21, 2026, 15:55 IST'
        assert page.inner_text('#clock').lower() == '24 h'
        # A link to 1 test opens that test, under the tab that holds it.
        skipped = records[4]
        linked = browser.new_page()
        linked.on('pageerror', lambda err: errors.append(str(err)))
        linked.goto(f'{path.as_uri()}#{skipped.safe}')
        assert linked.inner_text('#pane h3') == 'Snapshot saves'
        assert 'The camera has no SD card' in linked.inner_text('#pane section.on')
        assert linked.evaluate(VISIBLE_ROWS) == ['skip']
        assert linked.get_attribute(f'[id="{skipped.safe}"]', 'class') == 'row sel'
        browser.close()
    assert errors == []


def test_the_health_pages_link_to_a_runs_notes_opens_the_notes(tmp_path):
    """The health page links a module's note count to 'run.html#advisories' (health.render_health): that link opens the run's notes."""
    sync_api = pytest.importorskip('playwright.sync_api')
    path, _records = _browser_run(tmp_path)
    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(channel='chrome')
        except sync_api.Error:
            pytest.skip('no Chrome on this machine')
        page = browser.new_page()
        page.goto(f'{path.as_uri()}#advisories')
        heading = page.inner_text('#pane h3')
        browser.close()
    assert heading == 'Notes (1)'


def test_a_folder_stamp_is_a_time_too():
    """A run with no summary has only its folder name for a start: '2026-08-24_10-00-00' converts like the ISO form."""
    assert rp.utc_iso('2026-08-24_10-00-00', '-05:00') == '2026-08-24T15:00:00Z'
    assert rp.utc_iso('2026-08-24 10-00-00', '+00:00') == '2026-08-24T10:00:00Z'
