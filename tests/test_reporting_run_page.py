"""Unit tests for soterix_qa_shared.reporting.run_page.

The pure parts (the UTC stamp, the <time> element, the parameter of a node id, the
second axis, the line that several errors share, the tab of an outcome), then the
page on a small fake run folder with records built by hand, then the page's script
in a real Chrome. The browser pins skip on a machine with no Playwright or no Chrome.
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from html.parser import HTMLParser
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


def _main_of(page: str) -> str:
    """The left part of the page: the verdict, the facts, the causes, the map and the rows. The pane is not in it."""
    return page[page.index('<main>'):page.index('</main>')]


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


def _utc_by_this_machine(naive: str) -> str:
    """The UTC stamp of a time with no zone, by the zone of this machine on that date. The test computes it
    with datetime alone, so the pin holds on a machine in each zone."""
    return datetime.fromisoformat(naive).astimezone().astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')


@pytest.mark.parametrize('naive', ['2026-09-21T10:25:54', '2026-01-15T10:25:54'])
def test_utc_iso_without_an_offset_uses_this_machines(naive):
    """An older run recorded no offset: the stamp is by the offset of the machine that builds the page, on that
    date (a summer date and a winter date). An offset that is no offset reads the same."""
    here = _utc_by_this_machine(naive)
    assert rp.utc_iso(naive) == here
    assert rp.utc_iso(naive, '') == here
    assert rp.utc_iso(naive, 'odd') == here


STAMP_RE = r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z'


@pytest.mark.parametrize(('naive', 'offset', 'expected'), [
    # A year at the edge of the calendar: the conversion to UTC leaves the calendar.
    ('0001-01-01T00:00:00', '+05:30', ''),
    ('9999-12-31T23:59:59', '-05:00', ''),
    ('0001-01-01T00:00:00', '-05:00', '0001-01-01T05:00:00Z'),
    # With an offset, a time near 1970 needs no clock of the platform.
    ('1970-01-01T00:00:00', '+00:00', '1970-01-01T00:00:00Z'),
    # With no offset the platform places the time. Windows refuses these; Linux places some of them.
    ('0001-01-01T00:00:00', None, None),
    ('9999-12-31T23:59:59', None, None),
    ('1970-01-01T00:00:00', None, None),
    ('1969-12-31T20:00:00', None, None),
    # 'Z' and 'UTC' are +00:00.
    ('2026-09-21T10:25:54', 'Z', '2026-09-21T10:25:54Z'),
    ('2026-09-21T10:25:54', 'z', '2026-09-21T10:25:54Z'),
    ('2026-09-21T10:25:54', 'UTC', '2026-09-21T10:25:54Z'),
    ('2026-09-21T10:25:54', ' utc ', '2026-09-21T10:25:54Z'),
    # The widest offsets that are in range.
    ('2026-09-21T10:25:54', '+23:59', '2026-09-20T10:26:54Z'),
    ('2026-09-21T10:25:54', '-2359', '2026-09-22T10:24:54Z'),
])
def test_utc_iso_never_raises(naive, offset, expected):
    """A time that cannot be placed gives '', never an error: an error here cost the run its page.
    `expected` None: the answer is the platform's, so the pin is that the call returns '' or a stamp."""
    stamp = rp.utc_iso(naive, offset)
    if expected is None:
        assert stamp == '' or re.fullmatch(STAMP_RE, stamp)
    else:
        assert stamp == expected


@pytest.mark.parametrize('offset', ['+24:00', '+99:99', '+00:99', '-24:00', '+2400', '+1:00', 5, ['+05:30']])
def test_utc_iso_reads_an_offset_out_of_range_as_no_offset(offset):
    """An offset that no zone has does not raise. It reads as no offset: the stamp is by this machine's zone."""
    assert rp.utc_iso('2026-09-21T10:25:54', offset) == _utc_by_this_machine('2026-09-21T10:25:54')


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


def test_find_axis_is_none_when_a_cell_repeats():
    """A run on 2 browsers has 2 records for 1 (function, parameter) cell, and the map has 1 square for it.
    No map: the rows show each record."""
    records = _grid(('test_a', 'test_b'), CAMERAS[:2])
    assert rp.find_axis(records, {}) is not None
    twice = [*records, _rec(f'tests/ui/c2c/test_c.py::test_a[{CAMERAS[0]}-firefox]', 'failed')]
    assert rp.find_axis(twice, {}) is None
    # The same cell with an xdist group is the same cell.
    grouped = [*records, _rec(f'tests/ui/c2c/test_c.py::test_b[{CAMERAS[1]}-chromium]@account_c2c')]
    assert rp.find_axis(grouped, {}) is None


@pytest.mark.parametrize('source', ['summary', 'provider'])
def test_find_axis_copies_only_the_names_of_a_column(source):
    """The data of a column gives its group, label and title. A 'param' key in it cannot move the
    column to a different parameter, and no other key comes on the column."""
    named = {'noun': 'camera', 'columns': {CAMERAS[0]: {
        'param': CAMERAS[1], 'group': 'Lab', 'label': '.129', 'title': 'Lab at 10.2.0.129', 'rows': 'x', 'ip': '10.2.0.129'}}}
    if source == 'provider':
        rp.configure_axis(lambda params: named)
    axis = rp.find_axis(_grid(), {'axis': named} if source == 'summary' else {})
    assert sorted(c['param'] for c in axis['columns']) == sorted(CAMERAS)
    column = next(c for c in axis['columns'] if c['label'] == '.129')
    assert column == {'param': CAMERAS[0], 'group': 'Lab', 'label': '.129', 'title': 'Lab at 10.2.0.129'}
    # The cell of each row under that column is still the record of the first camera.
    position = axis['columns'].index(column)
    assert axis['rows'][0][2][axis['columns'][position]['param']].nodeid.endswith(f'[{CAMERAS[0]}-chromium]')


def test_find_axis_names_are_text_and_an_empty_name_keeps_the_fallback():
    """A name that is a number shows as text. An empty name, or None, keeps the name from the parameter text."""
    summary = {'axis': {'columns': {CAMERAS[0]: {'group': 7, 'label': '', 'title': None}}}}
    column = next(c for c in rp.find_axis(_grid(), summary)['columns'] if c['param'] == CAMERAS[0])
    assert column == {'param': CAMERAS[0], 'group': '7', 'label': '5b41', 'title': CAMERAS[0]}


@pytest.mark.parametrize('noun', [5, ['camera'], {'a': 1}, '', None, True])
def test_find_axis_noun_that_is_no_text_is_variant(noun):
    """The noun goes into sentences of the page ('2 cameras'). A noun that is no text falls back to 'variant'."""
    assert rp.find_axis(_grid(), {'axis': {'noun': noun, 'columns': {}}})['noun'] == 'variant'
    assert rp.find_axis(_grid(), {'axis': {'noun': 'camera', 'columns': {}}})['noun'] == 'camera'


@pytest.mark.parametrize(('param', 'group', 'label'), [
    ('dc-5v-1-q300c5b44', 'DC-5V-1', '5b44'),   # a serial: people name it by its end
    ('detect-person', 'DETECT', 'person'),   # a word stays whole
    ('detect-thermalcamera', 'DETECT', 'thermalcamera'),   # a long word too: it has no digit
    ('lab-cam12345', 'LAB', 'cam12345'),   # 8 characters with digits: short, so whole
    ('lab-cam123456', 'LAB', '3456'),   # 9 characters with a digit: cut to the end
    ('person', '', 'person'),   # 1 part: no group
])
def test_fallback_label_keeps_a_word_and_cuts_a_serial(param, group, label):
    """With no names given, the label is the last part of the parameter. Only a long part with a digit is cut to its last 4 characters."""
    assert rp._fallback_column(param) == {'group': group, 'label': label, 'title': param}


def test_find_axis_labels_a_word_parameter_whole():
    """The map of a suite that runs each test for 'person' and 'vehicle' heads its columns with those words."""
    records = [_rec(f'tests/ui/search/test_s.py::{fn}[chromium-detect-{kind}]')
               for fn in ('test_a', 'test_b') for kind in ('person', 'vehicle')]
    assert [c['label'] for c in rp.find_axis(records, {})['columns']] == ['person', 'vehicle']


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
    """Exit status 2: the counts alone do not say that the run was cut, so an Outcome fact leads the facts. A failed
    run (1) needs none. Run details give the outcome and the exit code of each run."""
    records = [_rec('tests/a.py::test_a[chromium]')]
    word = rr.EXIT_OUTCOMES[2]
    assert word == 'Interrupted'
    cut = _render(tmp_path, records, {'exitstatus': 2})
    assert f'<div class="facts"><div><span>Outcome</span><b class="tone-fail">{word}</b></div>' in cut
    assert f'<span>Outcome</span><span>{word}</span><span>Exit code</span><span>2</span>' in _named_template(cut, 't-run')
    failed = _render(tmp_path / 'other', records, {'exitstatus': 1})
    # The pane at rest holds Run details, so the search is in the left part of the page only.
    assert '<span>Outcome</span>' not in _main_of(failed)
    assert '<span>Outcome</span><span>Failed</span><span>Exit code</span><span>1</span>' in _named_template(failed, 't-run')
    # A run with no exit status has no Exit code row.
    assert 'Exit code' not in _render(tmp_path / 'none', records, {})


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


class _Tags(HTMLParser):
    """Each start tag with its attributes, and each end tag, as a browser's parser finds them. The parser
    reads the content of a <template> as tags too, and the content of <script> and <style> as text."""

    def __init__(self) -> None:
        super().__init__()
        self.starts: list[tuple[str, dict]] = []
        self.ends: list[str] = []

    def handle_starttag(self, tag, attrs):
        self.starts.append((tag, dict(attrs)))

    def handle_endtag(self, tag):
        self.ends.append(tag)


def _tags_of(page: str) -> _Tags:
    tags = _Tags()
    tags.feed(page)
    tags.close()
    return tags


# Text that closes an attribute, a template and the script, and then starts markup of its own.
BREAKOUT = ('x" onmouseover="alert({0})" y=\'z\' onfocus=\'alert({0})\' </template></script></aside>'
            '<img src=x onerror=alert({0})><script>alert({0})</script>')


def test_text_with_quotes_and_closing_tags_breaks_no_attribute_and_no_template(tmp_path):
    """A title, an error, a skip reason, a case title, a module, a product, a note, an axis name and a file
    name that hold quotes, '</template>' and '</script>': a parser finds only the tags that the page wrote.
    No attribute of a tag ends early (no tag gets an 'on...' attribute), the only <script> is the page's own,
    each <img> is the brand logo, and each <template> that opens closes: 1 for each test and 3 for the run."""
    failed = _rec(f'tests/ui/c2c/test_c.py::test_a[{CAMERAS[0]}-chromium]', 'failed',
                  title=BREAKOUT.format(1), headline=BREAKOUT.format(2), signature='sig', error=BREAKOUT.format(3),
                  log=BREAKOUT.format(4), advisories=[BREAKOUT.format(5)], case_key='K"1', case_title=BREAKOUT.format(6),
                  module=BREAKOUT.format(7), product=BREAKOUT.format(8), delta=BREAKOUT.format(9), digest={},
                  seen={'run': '2026-09-20_10-00-00__' + BREAKOUT.format(10), 'started': BREAKOUT.format(11)},
                  history=rr.history_facts([{'run': BREAKOUT.format(12), 'started': '2026-09-20T10:00:00',
                                             'utc_offset': '+00:00', 'outcome': BREAKOUT.format(13)}]))
    skipped = _rec(f'tests/ui/c2c/test_c.py::test_a[{CAMERAS[1]}-chromium]', 'skipped', reason=BREAKOUT.format(14),
                   title=BREAKOUT.format(15))
    odd = _rec('tests/ui/c2c/test_c.py::test_b["\'</template></script><img src=x>-chromium]', 'passed',
               duration=BREAKOUT.format(16))
    others = _grid(('test_b',), CAMERAS[:2])
    records = (failed, skipped, odd, *others)
    summary = {
        'started': '2026-09-21T05:25:54', 'utc_offset': '-05:00', 'exitstatus': BREAKOUT.format(17),
        'target': [BREAKOUT.format(18)], 'one_skip_reason': {'count': 1, 'reason': BREAKOUT.format(19)},
        'advisories': [{'test': BREAKOUT.format(20), 'line': BREAKOUT.format(21)}],
        'provenance': {'host': BREAKOUT.format(22), 'box': BREAKOUT.format(23), 'git_commit': BREAKOUT.format(24),
                       'browser': BREAKOUT.format(25), 'browser_latest': BREAKOUT.format(26)},
        'axis': {'noun': BREAKOUT.format(27), 'columns': {
            CAMERAS[0]: {'group': BREAKOUT.format(28), 'label': BREAKOUT.format(29), 'title': BREAKOUT.format(30)}}},
    }
    page = rp.render_run_page(_run_dir(tmp_path), summary, {r.nodeid: r for r in records},
                              ['screenshots/' + BREAKOUT.format(31) + '.png'],
                              previous={'name': BREAKOUT.format(32), 'started': BREAKOUT.format(33)},
                              product='Mixed: ' + BREAKOUT.format(34))
    assert 'id="map"' in page   # the axis names are on the page
    tags = _tags_of(page)
    assert [attrs for tag, attrs in tags.starts if tag == 'script'] == [{}]
    assert tags.ends.count('script') == 1
    assert all(attrs.get('class') == 'brand' for tag, attrs in tags.starts if tag == 'img')
    broken = [(tag, name) for tag, attrs in tags.starts for name in attrs if name.startswith('on') or name in ('y', 'x')]
    assert broken == []
    templates = [attrs.get('id') for tag, attrs in tags.starts if tag == 'template']
    assert templates == [f't{n}' for n in range(len(records))] + ['t-run', 't-notes', 't-slowest']
    assert tags.ends.count('template') == len(records) + 3
    assert (sum(1 for tag, _a in tags.starts if tag == 'aside'), tags.ends.count('aside')) == (1, 1)
    # Each value is on the page, as text.
    for n in (1, 2, 5, 6, 7, 8, 14, 21, 28, 29, 30, 31):
        assert f'&lt;script&gt;alert({n})&lt;/script&gt;' in page, n
    assert '&lt;/template&gt;' in page and '&quot; onmouseover=&quot;' in page


def test_times_of_an_earlier_run_convert_with_that_runs_offset_never_this_machines(tmp_path):
    """The central pin of the 09/21/2026 audit. 2 runs of 1 target, each with its recorded offset. The page of
    the newer run stamps each time of the earlier run with the earlier run's offset: 'last seen' on the cause,
    'Seen before' and the strip in the test's History tab, and 'Changes since' in Run details. The stamps are
    exact, so the pin holds on a machine in each zone: a page built on a laptop showed box times 5 hours off."""
    nodeid = 'tests/ui/c2c/test_c.py::test_stream_opens[chromium]'
    error = 'AssertionError: the stream did not open'
    for name, started in (('2026-09-20_22-30-00__tests-ui', '2026-09-20T22:30:00'),
                          ('2026-09-21_02-00-00__tests-ui', '2026-09-21T02:00:00')):
        (tmp_path / name).mkdir()
        (tmp_path / name / 'summary.json').write_text(json.dumps({
            'schema_version': 6, 'started': started, 'utc_offset': '+00:00', 'exitstatus': 1, 'target': ['tests/ui'],
            'counts': {'passed': 0, 'failed': 1, 'skipped': 0, 'errors': 0},
            'tests': [{'nodeid': nodeid, 'outcome': 'failed', 'error': error}]}), encoding='utf-8')
        (tmp_path / name / 'run.html').write_text('x', encoding='utf-8')
    page = rr.build_run_report(tmp_path / '2026-09-21_02-00-00__tests-ui')
    earlier = '<time data-utc="2026-09-20T22:30:00Z" data-short>Sep 20, 10:30 PM</time>'
    link = '<a href="../2026-09-20_22-30-00__tests-ui/run.html">'
    causes = re.findall(r'<details class="cause">.*?</details>', page, flags=re.DOTALL)
    assert len(causes) == 1
    assert f'last seen {earlier}' in causes[0]
    history = _tab(_template_of(page, rr._safe_name(nodeid, 120)), 2)
    assert f'Seen before, last on {link}{earlier}</a>' in history
    assert ('<a class="blk failed" href="../2026-09-20_22-30-00__tests-ui/run.html" data-utc="2026-09-20T22:30:00Z" '
            'data-label="Failed" title="Sep 20, 10:30 PM: Failed"></a>') in history
    assert ('<a class="blk failed" href="../2026-09-21_02-00-00__tests-ui/run.html" data-utc="2026-09-21T02:00:00Z" '
            'data-label="Failed" title="Sep 21, 2:00 AM: Failed"></a>') in history
    assert f'Changes since the {link}previous run of these tests</a>, {earlier}.' in _named_template(page, 't-run')
    assert '<time data-utc="2026-09-21T02:00:00Z">Sep 21, 2026, 2:00 AM</time>' in _main_of(page)
    # No other stamp of the earlier day is on the page: a stamp by a different zone would be one.
    assert set(re.findall(r'data-utc="(2026-09-20[^"]*)"', page)) == {'2026-09-20T22:30:00Z'}


def test_each_run_of_the_strip_converts_with_its_own_offset(tmp_path):
    """The box ran the earlier run in India (+05:30) and this run in Central time (-05:00): each time of the
    earlier run is by +05:30, and the times of this run are by -05:00."""
    nodeid = 'tests/ui/c2c/test_c.py::test_stream_opens[chromium]'
    for name, started, offset in (('2026-09-20_22-30-00__tests-ui', '2026-09-20T22:30:00', '+05:30'),
                                  ('2026-09-21_02-00-00__tests-ui', '2026-09-21T02:00:00', '-05:00')):
        (tmp_path / name).mkdir()
        (tmp_path / name / 'summary.json').write_text(json.dumps({
            'schema_version': 6, 'started': started, 'utc_offset': offset, 'exitstatus': 1,
            'tests': [{'nodeid': nodeid, 'outcome': 'failed', 'error': 'assert 1 == 2'}]}), encoding='utf-8')
    page = rr.build_run_report(tmp_path / '2026-09-21_02-00-00__tests-ui')
    assert set(re.findall(r'data-utc="(2026-09-20[^"]*)"', page)) == {'2026-09-20T17:00:00Z'}
    assert page.count('data-utc="2026-09-20T17:00:00Z"') >= 4   # the cause, Seen before, the strip, Changes since
    assert set(re.findall(r'data-utc="(2026-09-21[^"]*)"', page)) == {'2026-09-21T07:00:00Z'}


def test_rows_with_one_safe_name_get_different_ids(tmp_path):
    """2 node ids that share their first 120 safe characters: the second row's id gets its index, so each
    id is on the page 1 time and a link to each row opens its own test."""
    long_name = 'tests/ui/c2c/test_c.py::test_' + 'x' * 130
    first, second = _rec(f'{long_name}_one[chromium]', title='First'), _rec(f'{long_name}_two[chromium]', 'failed', title='Second')
    assert first.safe == second.safe and len(first.safe) == rr.SAFE_NAME_MAX
    page = _render(tmp_path, [first, second])
    rows = re.findall(r'<div class="row"[^>]* id="([^"]*)" data-id="(\d+)"', page)
    assert rows == [(first.safe, '0'), (f'{first.safe}-1', '1')]
    assert '<h3>First</h3>' in _template_of(page, first.safe)
    assert '<h3>Second</h3>' in _template_of(page, f'{first.safe}-1')
    ids = [attrs['id'] for _tag, attrs in _tags_of(_outside_templates(page)).starts if 'id' in attrs]
    assert len(ids) == len(set(ids))


def test_a_row_id_with_an_index_is_not_the_safe_name_of_a_different_test(tmp_path):
    """'[a b]' and '[a-b]' share a safe name, so the second gets '-<index>'. A third test whose own safe name
    is that text must not share the id: a link to it opens 1 test."""
    records = [_rec('tests/a.py::test_x[a b]'), _rec('tests/a.py::test_x[a-b-2]'), _rec('tests/a.py::test_x[a-b]')]
    assert [r.safe for r in records] == ['tests-a.py-test_x-a-b', 'tests-a.py-test_x-a-b-2', 'tests-a.py-test_x-a-b']
    ids = re.findall(r'<div class="row"[^>]* id="([^"]*)" data-id="\d+"', _render(tmp_path, records))
    assert len(ids) == 3
    assert len(set(ids)) == 3, ids


def test_the_pane_holds_run_details_with_no_script(tmp_path):
    """The source of the page has Run details in the pane, so a reader with no script sees the run's facts.
    The pane is no live region: a screen reader does not read each test that opens as an alert."""
    page = _render(tmp_path, [_rec('tests/a.py::test_a[chromium]')], {'exitstatus': 1, 'started': '2026-09-21T05:25:54'})
    aside = re.search(r'<aside id="pane">(.*?)</aside>', page, flags=re.DOTALL)
    assert aside, 'the pane has an attribute that the pin does not know'
    assert '<h3>Run details</h3>' in aside.group(1)
    assert '<span>Outcome</span><span>Failed</span><span>Exit code</span><span>1</span>' in aside.group(1)
    assert aside.group(1) == _named_template(page, 't-run')   # the same content that the script shows at rest
    assert 'aria-live' not in page


def test_the_row_glyph_and_the_map_square_say_the_outcome(tmp_path):
    """The glyph of a row is an image with the outcome as its name. Each map cell with a test carries the
    test's index on the cell and on its button, so a click beside the small square opens the test too."""
    records = _grid(('test_a', 'test_b'), CAMERAS[:2])
    records[0].outcome, records[3].outcome = 'failed', 'skipped'
    records.append(_rec('tests/ui/c2c/test_c.py::test_c[chromium]', 'unknown'))
    page = _render(tmp_path, records)
    for outcome, css, label in (('failed', 'fail', 'Failed'), ('skipped', 'skip', 'Skipped'), ('passed', 'pass', 'Passed'),
                                ('unknown', 'unkn', rr.outcome_label('unknown'))):
        assert f'data-o="{css}" data-test=' in page
        assert re.search(rf'data-o="{css}" data-test="[^"]*"><span class="g {css}" role="img" aria-label="{label}"></span>', page), outcome
    cells = re.findall(r'<td(?: class="edge")? data-col="\d+" data-id="(\d+)"><button type="button" class="g \w+" data-id="(\d+)"', page)
    assert len(cells) == 4
    assert all(cell == button for cell, button in cells)
    assert sorted(cell for cell, _b in cells) == ['0', '1', '2', '3']


def test_the_verdict_counts_each_row_and_says_which_failures_are_errors(tmp_path):
    """The total of the verdict is the count of the All tab: a test with no outcome is in it. The line under
    the verdict says how many failures are errors (the test could not start) and how many tests have no outcome."""
    records = [_rec('tests/a.py::test_a[chromium]'), _rec('tests/a.py::test_b[chromium]', 'unknown'),
               _rec('tests/a.py::test_c[chromium]', 'unknown')]
    page = _render(tmp_path, records)
    assert '<p class="verdict"><em class="tone-pass">1 of 3 passed.</em></p>' in page
    assert '2 tests have no recorded outcome.' in _main_of(page)
    assert 'All<b>3</b>' in page
    records += [_rec('tests/a.py::test_d[chromium]', 'error', signature='s1', headline='fixture failed'),
                _rec('tests/a.py::test_e[chromium]', 'failed', signature='s2', headline='assert 1 == 2')]
    records[1].outcome = 'passed'
    page = _render(tmp_path / 'errors', records)
    assert '<em class="tone-fail">2 of 5 failed.</em> 2 causes.' in page
    sub = re.search(r'<p class="sub">(.*?)</p>', _main_of(page)).group(1)
    assert '1 failure is an error: the test could not start.' in sub
    assert '1 test has no recorded outcome.' in sub
    records[4].outcome = 'error'
    assert '2 failures are errors: the test could not start.' in _render(tmp_path / 'two', records)
    # A run with no error and no unknown outcome says neither.
    plain = _render(tmp_path / 'plain', [_rec('tests/a.py::test_a[chromium]'), _rec('tests/a.py::test_b[chromium]', 'failed')])
    assert 'could not start' not in plain and 'no recorded outcome' not in _main_of(plain)


def test_a_video_has_a_link_beside_the_player(tmp_path):
    """The player needs a browser that plays WebM. The 'Video clip' link opens or saves the file in each browser."""
    rec = _rec('tests/a.py::test_a[chromium]', 'failed', digest={}, videos=['traces/a b-video.webm'])
    evidence = _tab(_template_of(_render(tmp_path, [rec]), rec.safe), 1)
    assert ('<video controls preload="none" src="traces/a%20b-video.webm"></video>'
            '<div><a href="traces/a%20b-video.webm">Video clip</a></div>') in evidence


def test_the_style_shows_the_keyboard_focus():
    """The buttons of the page unset the browser's style, so the page gives them a focus outline again.
    The dim text colour on the light page is #5f646c."""
    css = rp.RUN_PAGE_CSS
    rule = re.search(r'([^{}]*)\{ outline:2px solid var\(--ink\); outline-offset:2px; \}', css)
    assert rule
    for selector in ('nav button', '.panelinks button', '.tabs button', '#clock', '#maptoggle', '#close', '#filter'):
        assert f'{selector}:focus-visible' in rule.group(1)
    assert css.count('--dim:#5f646c') == 1
    assert 'body.noscroll { overflow:hidden; }' in css


# ---------------------------------------------------------------------------
# The page's script, in Chrome
# ---------------------------------------------------------------------------

def _browser_run(tmp_path: Path) -> tuple[Path, list[rr.TestRecord]]:
    """3 tests on 3 cameras: 'Stream opens' fails on 2 cameras with 1 error, 'Snapshot saves' is
    skipped on 1, 'Reboot works' passes on each. The first failure has a history of 2 runs, each with
    its offset. Written to run.html in a run folder."""
    titles = {'test_a': 'Stream opens', 'test_b': 'Snapshot saves', 'test_c': 'Reboot works'}
    records = _grid()
    for rec in records:
        rec.title = titles[rec.nodeid.split('::')[1].split('[')[0]]
    for rec, address in zip(records[:2], ('10.2.0.129', '10.2.0.171')):
        rec.outcome, rec.signature, rec.headline = 'failed', 'Camera # reports no stream', f'Camera {address} reports no stream'
    records[4].outcome, records[4].reason = 'skipped', 'The camera has no SD card'
    records[0].history = rr.history_facts([
        {'run': '2026-09-21_10-00-00__tests-ui', 'started': '2026-09-21T05:25:54', 'utc_offset': '-05:00', 'outcome': 'failed'},
        {'run': '2026-09-20_22-30-00__tests-ui', 'started': '2026-09-20T22:30:00', 'utc_offset': '+00:00', 'outcome': 'passed'}])
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


@pytest.fixture
def chrome():
    """open_page(url, width, height) in a real Chrome, and the list of the script errors of each page
    that it opened. Skips on a machine with no Playwright or no Chrome."""
    sync_api = pytest.importorskip('playwright.sync_api')
    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(channel='chrome')
        except sync_api.Error:
            pytest.skip('no Chrome on this machine')
        errors: list[str] = []

        def open_page(url: str, width: int = 1400, height: int = 900):
            page = browser.new_page(viewport={'width': width, 'height': height})
            page.on('pageerror', lambda err: errors.append(str(err)))
            page.goto(url)
            return page

        yield open_page, errors
        browser.close()


ACTIVE_ID = '() => document.activeElement && document.activeElement.id'
NOSCROLL = "() => document.body.classList.contains('noscroll')"


def test_on_a_narrow_screen_a_link_to_a_test_opens_the_pane_and_close_gives_the_focus_back(tmp_path, chrome):
    """At 800 by 700 the pane covers the page. A link to '#<row id>' shows it, with the page behind held
    still (body.noscroll). Close hides it, lets the page scroll and puts the focus on the row. Enter on the row
    opens it again with the focus on Close, and Escape closes it the same way."""
    open_page, errors = chrome
    path, records = _browser_run(tmp_path)
    failed = records[0]
    page = open_page(f'{path.as_uri()}#{failed.safe}', 800, 700)
    assert page.evaluate("() => getComputedStyle(document.querySelector('#pane')).position") == 'fixed'
    assert page.is_visible('#pane')
    assert page.inner_text('#pane h3') == 'Stream opens'
    assert page.evaluate(NOSCROLL) is True
    assert page.evaluate("() => getComputedStyle(document.body).overflow") == 'hidden'
    page.click('#close')
    assert page.is_hidden('#pane')
    assert page.evaluate(NOSCROLL) is False
    assert page.evaluate(ACTIVE_ID) == failed.safe
    # By the keyboard: Enter on the row opens the pane, Escape closes it and the focus is on the row again.
    page.keyboard.press('Enter')
    assert page.is_visible('#pane')
    assert page.evaluate(NOSCROLL) is True
    assert page.evaluate(ACTIVE_ID) == 'close'
    assert page.evaluate("() => getComputedStyle(document.activeElement).outlineStyle") != 'none'
    page.keyboard.press('Escape')
    assert page.is_hidden('#pane')
    assert page.evaluate(NOSCROLL) is False
    assert page.evaluate(ACTIVE_ID) == failed.safe
    # A new hash on the open page opens the pane with the focus on Close, and Close gives it to that row.
    page.evaluate('(id) => { location.hash = "#" + id; }', records[4].safe)
    page.wait_for_function("() => document.querySelector('#pane h3').textContent === 'Snapshot saves'")
    assert page.is_visible('#pane')
    assert page.evaluate(ACTIVE_ID) == 'close'
    page.click('#close')
    assert page.evaluate(ACTIVE_ID) == records[4].safe
    # With no link the pane stays shut: the page opens on the list.
    plain = open_page(path.as_uri(), 800, 700)
    assert plain.is_hidden('#pane')
    assert plain.evaluate(NOSCROLL) is False
    # The link to the notes opens the pane too.
    notes = open_page(f'{path.as_uri()}#advisories', 800, 700)
    assert notes.is_visible('#pane')
    assert notes.inner_text('#pane h3') == 'Notes (1)'
    assert errors == []


def test_on_a_narrow_screen_a_link_to_a_test_puts_the_focus_on_close(tmp_path, chrome):
    """The pane covers the page, so the focus must be in it: on Close. When the page loads from a link to a
    row, the browser moves the focus to the target of the link after the script ran. The pin reads the focus
    after the load, where the reader finds it."""
    open_page, errors = chrome
    path, records = _browser_run(tmp_path)
    page = open_page(f'{path.as_uri()}#{records[0].safe}', 800, 700)
    assert page.is_visible('#pane')
    assert page.evaluate(ACTIVE_ID) == 'close'
    assert errors == []


def test_the_tab_of_the_list_that_is_open_says_pressed_from_the_start(tmp_path, chrome):
    """The tabs of the list are toggle buttons. A screen reader hears which one is pressed when the page
    opens, before a click: the first tab says true and each other tab says false."""
    open_page, errors = chrome
    path, _records = _browser_run(tmp_path)
    page = open_page(path.as_uri())
    pressed = "() => [...document.querySelectorAll('nav button')].map(b => [b.dataset.f, b.getAttribute('aria-pressed')])"
    assert page.evaluate(pressed) == [['fail', 'true'], ['skip', 'false'], ['pass', 'false'], ['all', 'false']]
    page.click('nav button[data-f="all"]')
    assert page.evaluate(pressed) == [['fail', 'false'], ['skip', 'false'], ['pass', 'false'], ['all', 'true']]
    assert errors == []


def test_a_new_hash_on_the_open_page_opens_that_test(tmp_path, chrome):
    """A reader pastes a link to a different test into the tab that has the page: the hash changes with no
    load, and the page opens that test under its tab. On a wide screen the pane is beside the list, so the
    page scrolls and the focus stays."""
    open_page, errors = chrome
    path, records = _browser_run(tmp_path)
    failed, skipped = records[1], records[4]
    page = open_page(f'{path.as_uri()}#{failed.safe}')
    assert 'DC-5V-1 5b42' in page.text_content('#pane .lab')
    assert page.evaluate(VISIBLE_ROWS) == ['fail', 'fail']
    assert page.evaluate(NOSCROLL) is False
    assert page.evaluate(ACTIVE_ID) != 'close'
    page.evaluate('(id) => { location.hash = "#" + id; }', skipped.safe)
    page.wait_for_function("() => document.querySelector('#pane h3').textContent === 'Snapshot saves'")
    assert 'The camera has no SD card' in page.inner_text('#pane section.on')
    assert page.evaluate(VISIBLE_ROWS) == ['skip']
    assert page.get_attribute(f'[id="{skipped.safe}"]', 'class') == 'row sel'
    assert page.get_attribute(f'[id="{failed.safe}"]', 'class') == 'row off'
    assert page.get_attribute('nav button[data-f="skip"]', 'aria-pressed') == 'true'
    assert page.get_attribute('nav button[data-f="fail"]', 'aria-pressed') == 'false'
    # A hash that names no row changes nothing.
    page.evaluate('() => { location.hash = "#no-such-row"; }')
    page.evaluate('() => new Promise(done => setTimeout(done, 50))')
    assert page.inner_text('#pane h3') == 'Snapshot saves'
    assert errors == []


def test_escape_in_the_filter_box_clears_the_box_first(tmp_path, chrome):
    """Escape in the filter box with text in it clears the box, and the open test stays. Escape with the box
    empty does what Escape does on the rest of the page: the pane goes back to Run details."""
    open_page, errors = chrome
    path, _records = _browser_run(tmp_path)
    page = open_page(path.as_uri())
    page.click('#list .row[data-o="fail"] >> nth=0')
    assert page.inner_text('#pane h3') == 'Stream opens'
    page.click('#filter')
    page.keyboard.type('5b41')
    assert page.evaluate(VISIBLE_ROWS) == ['fail']
    page.keyboard.press('Escape')
    assert page.inner_text('#pane h3') == 'Stream opens'
    assert page.input_value('#filter') == ''
    assert page.evaluate(VISIBLE_ROWS) == ['fail', 'fail']
    assert page.evaluate(ACTIVE_ID) == 'filter'
    page.keyboard.press('Escape')
    assert page.inner_text('#pane h3') == 'Run details'
    assert errors == []


def test_the_clock_switch_moves_the_strip_tooltips_and_the_tab_title(tmp_path, chrome):
    """The tooltips of a history strip in the pane, and the title of the browser tab, are times too: they are
    on the clock of the heading, before and after the switch. A test that opens after the switch is on it too."""
    open_page, errors = chrome
    path, records = _browser_run(tmp_path)
    page = open_page(f'{path.as_uri()}#{records[0].safe}')
    pressed = "() => [...document.querySelectorAll('#pane [data-tab]')].map(b => b.getAttribute('aria-pressed'))"
    assert page.evaluate(pressed) == ['true', 'false', 'false', 'false']
    page.click('#pane [data-tab="2"]')
    assert page.evaluate(pressed) == ['false', 'false', 'true', 'false']
    tips = "() => [...document.querySelectorAll('#pane .blk')].map(b => b.title)"
    assert re.search(r' (AM|PM) ', page.text_content('h1 time'))
    assert page.evaluate(tips) == ['Sep 20, 5:30 PM: Passed', 'Sep 21, 5:25 AM: Failed']
    assert re.fullmatch(r'Test Run - Sep 21, 2026, 5:25 AM (CDT|CST)', page.title())
    page.click('#clock')
    assert not re.search(r'AM|PM', page.text_content('h1 time'))
    after = page.evaluate(tips)
    assert after == ['Sep 21, 04:00: Passed', 'Sep 21, 15:55: Failed']
    assert not any(re.search(r'AM|PM', tip) for tip in after)
    assert page.title() == 'Test Run - Sep 21, 2026, 15:55 IST'
    # The pane is written again for each test that opens: its tooltips are on the 24 hour clock from the start.
    page.keyboard.press('Escape')
    page.click(f'[id="{records[0].safe}"]')
    assert page.evaluate(tips) == after
    page.click('#clock')
    assert all(re.search(r' (AM|PM): ', tip) for tip in page.evaluate(tips))
    assert re.fullmatch(r'Test Run - Sep 21, 2026, 5:25 AM (CDT|CST)', page.title())
    assert errors == []


def test_a_click_in_a_map_cell_beside_its_square_opens_the_test(tmp_path, chrome):
    """The square of the map is 11 pixels wide in a cell of 52: a click in the cell that misses the square
    opens the test of that cell."""
    open_page, errors = chrome
    path, records = _browser_run(tmp_path)
    passed = records[2]   # 'Stream opens' on the third camera
    page = open_page(path.as_uri())
    index = page.get_attribute(f'[id="{passed.safe}"]', 'data-id')
    box = page.locator(f'#map td[data-id="{index}"]').bounding_box()
    x, y = box['x'] + 4, box['y'] + box['height'] / 2
    assert page.evaluate('([x, y]) => document.elementFromPoint(x, y).tagName', [x, y]) == 'TD'
    assert page.inner_text('#pane h3') == 'Run details'
    page.mouse.click(x, y)
    assert page.inner_text('#pane h3') == 'Stream opens'
    assert 'BC-5V-1 5b43' in page.text_content('#pane .lab')
    assert 'sel' in page.get_attribute(f'#map td[data-id="{index}"] button', 'class')
    # A cell with no test opens nothing, and a click on the head of a column opens nothing.
    page.keyboard.press('Escape')
    page.click('#map thead th.cam >> nth=0')
    assert page.inner_text('#pane h3') == 'Run details'
    assert errors == []


def test_the_tab_key_shows_where_the_focus_is(tmp_path, chrome):
    """The buttons of the page unset the browser's style, and the focus ring went with it. Each control that the
    Tab key reaches shows an outline: the tabs of the list, the filter box, the clock, the pane links, the map
    switch and the tabs of the pane."""
    open_page, errors = chrome
    path, records = _browser_run(tmp_path)
    page = open_page(f'{path.as_uri()}#{records[0].safe}')
    kinds = ('nav button', '.panelinks button', '.tabs button', '#clock', '#maptoggle', '#filter')
    probe = ('(kinds) => { var el = document.activeElement; var kind = kinds.find(k => el.matches(k)); '
             'return kind ? [kind, el.matches(":focus-visible"), getComputedStyle(el).outlineStyle, '
             'getComputedStyle(el).outlineWidth] : null; }')
    seen: dict[str, list] = {}
    for _press in range(80):
        page.keyboard.press('Tab')
        found = page.evaluate(probe, list(kinds))
        if found:
            seen.setdefault(found[0], found[1:])
        if len(seen) == len(kinds):
            break
    assert set(seen) == set(kinds)
    for kind, (focus_visible, style, width) in seen.items():
        assert focus_visible is True, kind
        assert style != 'none', kind
        assert width == '2px', kind
    assert errors == []


def test_a_folder_stamp_is_a_time_too():
    """A run with no summary has only its folder name for a start: '2026-08-24_10-00-00' converts like the ISO form."""
    assert rp.utc_iso('2026-08-24_10-00-00', '-05:00') == '2026-08-24T15:00:00Z'
    assert rp.utc_iso('2026-08-24 10-00-00', '+00:00') == '2026-08-24T10:00:00Z'
