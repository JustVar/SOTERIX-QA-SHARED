"""Unit tests for soterix_qa_shared.reporting.health.

Aggregation over fake summaries: the per-module window, runs of one
test pooled into their module, pass rates over test outcomes, mean
full-module durations, advisory counts, the tests to watch with their
history strips and flaky rates, the verdict lines, the wording of what
each run ran, tolerance of summaries written before per-test records
existed, the page (intro lines, tiles, module rows, run lists, links),
the terminal table, and the CLI on a temp archive. No browser, no
network.
"""
from __future__ import annotations

import json
import re

import pytest

from soterix_qa_shared.reporting import health as hs


@pytest.fixture(autouse=True)
def _c2c_path_rule():
    """The consumer's products shim configures this rule at import (c2c tests
    map to the Camera 2 Cloud product). The package test configures it the
    same way, so product resolution matches the consumer exactly."""
    from soterix_qa_shared.reporting import products
    products.configure_path_rules([('tests/ui/c2c/', '60W5')])
    yield
    products.configure_path_rules([])

A = 'tests/ui/views/test_a.py::test_one'
B = 'tests/ui/views/test_a.py::test_two'
C = 'tests/ui/views/test_a.py::test_three'
M = 'tests/ui/media/test_m.py::test_m'
TITLES = {A: 'Open a view', B: 'Rename a view', C: 'Delete a view', M: 'Play a clip'}
RUN_NAME_RE = re.compile(r'\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}__')


def _t(nodeid: str, outcome: str, seconds: float, module: str = 'ui/views/test_a.py') -> dict:
    return {'nodeid': nodeid, 'module': module, 'title': TITLES[nodeid],
            'outcome': outcome, 'duration_s': seconds}


def _run(name: str, counts: dict, tests: list | None = None, advisories: list | None = None,
         duration: float = 100.0, exitstatus: int = 0, target: list | None = None,
         markexpr: str = '') -> dict:
    summary = {'schema_version': 2, 'started': name[:19].replace('_', 'T'),
               'duration_s': duration, 'exitstatus': exitstatus, 'counts': counts,
               'target': target or ['tests/ui/views'], 'markexpr': markexpr,
               'advisories': advisories or []}
    if tests is not None:
        summary['tests'] = tests
    return {'name': name, 'summary': summary}


# Views: two whole-module runs, one run of a single test with records, and
# one run of a single test archived before per-test records existed.
# Media: one whole-module run.
RUNS = [
    _run('2026-08-24_12-00-00__views', {'passed': 2, 'failed': 1},
         tests=[_t(A, 'failed', 30), _t(B, 'passed', 5), _t(C, 'passed', 2)],
         advisories=[{'test': A, 'phase': 'call', 'line': '[VIEWS] x'}], exitstatus=1),
    _run('2026-08-23_12-00-00__views', {'passed': 3},
         tests=[_t(A, 'passed', 20), _t(B, 'passed', 6), _t(C, 'passed', 4)]),
    _run('2026-08-22_12-00-00__views-test_a.py-test_one', {'passed': 1},
         tests=[_t(A, 'passed', 25)], duration=30, target=[A]),
    _run('2026-08-21_12-00-00__views-test_a.py-test_two', {'failed': 1},
         duration=20, exitstatus=1, target=[B]),  # schema 1: no per-test records
    _run('2026-08-24_09-00-00__media', {'passed': 1},
         tests=[_t(M, 'passed', 9, 'ui/media/test_m.py')], duration=50,
         target=['tests/ui/media']),
]


@pytest.mark.parametrize(('path', 'key', 'name'), [
    ('tests/ui/views', 'views', 'Views'),
    ('tests/ui/views/test_a.py::TestA::test_one[chromium]', 'views', 'Views'),
    ('ui/views/test_a.py', 'views', 'Views'),
    ('tests\\ui\\admin\\test_admin_sites.py', 'admin', 'Admin'),
    ('tests/tools/test_qatouch_client.py', 'tools', 'Tools'),
    ('tests/ui/lpr_search', 'lpr_search', 'Lpr search'),
    ('tests', '', 'All tests'),
    ('tests/ui', '', 'All tests'),
    ('tests/test_root.py', '', 'All tests'),
    ('', '', 'All tests'),
    ('other/path.py', 'other', 'Other'),
])
def test_module_key_is_the_folder_under_tests(path, key, name):
    """A test path or target names its module: the folder under tests/,
    with ui/ dropped, and the tests root as the whole suite."""
    assert hs.module_key(path) == key
    assert hs.module_name(key) == name


def test_run_modules_come_from_records_else_targets():
    """A run's modules are those of its per-test records, else those its
    targets name, each once. A run with neither ran the whole suite."""
    assert hs.run_modules(RUNS[0]['summary']) == ['views']
    assert hs.run_modules(RUNS[3]['summary']) == ['views']
    assert hs.run_modules({'target': ['tests/ui/media', 'tests/ui/maps', 'tests/ui/media']}) == \
        ['media', 'maps']
    assert hs.run_modules({'target': ['tests/ui/admin/x.py'],
                           'tests': [{'nodeid': M}]}) == ['media']
    assert hs.run_modules({}) == ['']
    assert hs.run_modules({'target': 'tests/ui/views'}) == ['']  # not a list: ignored


def test_window_keeps_the_newest_n_of_each_module():
    """The window keeps the newest N runs of each module, newest first,
    so a run of one test counts toward its module's N."""
    window = hs.select_window(RUNS, 2)
    assert [r['name'] for r in window] == [
        '2026-08-24_12-00-00__views', '2026-08-24_09-00-00__media', '2026-08-23_12-00-00__views']
    assert len(hs.select_window(RUNS, 20)) == 5
    assert hs.select_window([], 20) == []


def test_single_test_runs_pool_into_their_module():
    """Every run whose records or targets fall in a module counts toward
    it: runs, outcomes, notes, the newest run, and the run list."""
    agg = hs.aggregate(RUNS, 20)
    assert [m['module'] for m in agg['modules']] == ['media', 'views']
    views = agg['modules'][1]
    assert views['name'] == 'Views'
    assert views['runs'] == 4  # two whole-module runs and two runs of one test
    assert (views['passed'], views['failed']) == (6, 2)
    assert views['advisories'] == 1
    assert views['newest_run'] == '2026-08-24_12-00-00__views'
    assert views['newest_started'] == '2026-08-24T12-00-00'
    assert views['newest_exit'] == 1
    assert views['product'] == 'NexaiQ-Core'
    assert views['mixed'] is False
    assert [(r['run'], r['records']) for r in views['run_list']] == [
        ('2026-08-24_12-00-00__views', True), ('2026-08-23_12-00-00__views', True),
        ('2026-08-22_12-00-00__views-test_a.py-test_one', True),
        ('2026-08-21_12-00-00__views-test_a.py-test_two', False)]
    media = agg['modules'][0]
    assert (media['runs'], media['passed'], media['failed']) == (1, 1, 0)
    assert 'targets' not in agg


def test_pass_rate_is_over_test_outcomes_not_runs():
    """The rate counts passed and failed tests: the failing whole-module
    run is 1 of 3 tests, not 1 of 1 run. A run without records adds its
    totals to each module its targets name."""
    agg = hs.aggregate(RUNS, 20)
    views = next(m for m in agg['modules'] if m['module'] == 'views')
    assert views['pass_rate'] == 0.75  # 6 passed / (6 + 2), over 4 runs of which 2 failed
    assert views['unrecorded_failed'] == 1
    two = hs.aggregate([_run('2026-08-01_10-00-00__two', {'passed': 2},
                             target=[A, 'tests/ui/media/test_m.py::test_m'])], 20)
    assert {m['module']: (m['runs'], m['passed']) for m in two['modules']} == {
        'media': (1, 2), 'views': (1, 2)}


def test_mean_full_run_counts_only_whole_module_runs():
    """The mean duration is over runs of the module's own folder with no
    marker: a run of one test or a marker run is not the module's time."""
    agg = hs.aggregate(RUNS, 20)
    by_key = {m['module']: m for m in agg['modules']}
    assert (by_key['views']['mean_full_run_s'], by_key['views']['full_runs']) == (100.0, 2)
    assert (by_key['media']['mean_full_run_s'], by_key['media']['full_runs']) == (50.0, 1)
    marked = hs.aggregate([_run('2026-08-01_10-00-00__views-dragdrop', {'passed': 2},
                                markexpr='dragdrop')], 20)
    assert marked['modules'][0]['mean_full_run_s'] is None
    assert hs._is_full_module_run({'target': ['tests']}, '') is True
    assert hs._is_full_module_run({'target': ['tests/ui/views/']}, 'views') is True
    assert hs._is_full_module_run({'target': ['tests/ui/views/sub']}, 'views') is False


@pytest.mark.parametrize(('summary', 'words'), [
    ({'target': ['tests/ui/views']}, 'whole module'),
    ({'target': ['tests/ui/views/']}, 'whole module'),
    ({'target': ['tests/ui/views'], 'markexpr': 'dragdrop'}, 'marker: dragdrop'),
    ({'target': [A]}, '1 test: Open a view'),
    ({'target': ['tests/ui/views/test_a.py::TestA::test_drag_camera_to_tile[chromium]']},
     '1 test: Drag camera to tile'),
    ({'target': [A, B]}, '2 tests: Open a view + Rename a view'),
    ({'target': [C]}, '1 test: Delete a view'),  # the recorded title's period is dropped
    ({'target': ['tests/ui/admin/test_admin_sites.py']}, '1 file: test_admin_sites.py'),
    ({'target': ['tests\\ui\\admin\\test_admin_sites.py', 'tests/ui/admin/test_admin_users.py']},
     '2 files: test_admin_sites.py + test_admin_users.py'),
    ({'target': ['tests/ui/admin/test_admin_sites.py'], 'markexpr': 'smoke'},
     '1 file: test_admin_sites.py, marker: smoke'),
    ({'target': [A, 'tests/ui/admin/test_admin_sites.py']},
     '1 test: Open a view, 1 file: test_admin_sites.py'),
    ({'target': ['tests/ui/media', 'tests/ui/maps']}, 'all of Media + all of Maps'),
    ({'target': ['tests/ui/views/sub']}, 'folder: sub'),
    ({'target': ['tests']}, 'all tests'),
    ({'target': []}, 'all tests'),
    ({}, 'all tests'),
    ({'markexpr': 'smoke'}, 'marker: smoke'),
    ({'target': ['tests'], 'keyword': 'login'}, 'keyword: login'),
    ({'target': [A], 'markexpr': 'smoke', 'keyword': 'open'},
     '1 test: Open a view, marker: smoke, keyword: open'),
])
def test_run_scope_reads_as_words(summary, words):
    """What a run ran, in words: a test by its recorded title (else its
    function name as words), a file by its name, a folder as the whole
    module. Never a node id."""
    assert hs.run_scope(summary, {A: 'Open a view', B: 'Rename a view', C: 'Delete a view.'}) == words
    assert '::' not in words


def test_run_list_names_single_tests_by_their_recorded_title():
    """A run of one test reads by the title its records carry, and a run
    archived without records borrows the title a newer run recorded."""
    agg = hs.aggregate(RUNS, 20)
    views = next(m for m in agg['modules'] if m['module'] == 'views')
    assert [r['scope'] for r in views['run_list']] == [
        'whole module', 'whole module', '1 test: Open a view', '1 test: Rename a view']
    alone = hs.aggregate([RUNS[3]], 20)  # no record of B anywhere: its function name as words
    assert alone['modules'][0]['run_list'][0]['scope'] == '1 test: Two'


def test_tests_to_watch_carry_strips_rates_and_last_failures():
    """Every test that failed in the window is listed with its history
    facts, sorted by flaky rate, and each module counts its own."""
    agg = hs.aggregate(RUNS, 20)
    watch = {w['nodeid']: w for w in agg['watch']}
    assert [w['nodeid'] for w in agg['watch']] == [A]
    a = watch[A]['facts']
    assert [b['outcome'] for b in a['blocks']] == ['passed', 'passed', 'failed']  # oldest first
    assert (a['runs'], a['passed'], a['failed'], a['flaky']) == (3, 2, 1, True)
    assert round(a['rate'], 3) == 0.333
    assert a['last_failure']['run'] == '2026-08-24_12-00-00__views'
    assert a['last_failure']['started'] == '2026-08-24T12-00-00'
    assert watch[A]['title'] == 'Open a view'
    assert watch[A]['module'] == 'ui/views/test_a.py'
    assert watch[A]['module_name'] == 'Views'
    assert B not in watch  # its failure is in a run without records
    assert C not in watch  # never failed
    assert M not in watch
    assert {m['module']: m['watch'] for m in agg['modules']} == {'views': 1, 'media': 0}
    for key in ('flaky', 'newly_failing', 'slowest', 'targets'):
        assert key not in agg


def test_window_changes_the_verdicts():
    """With only the newest run per module, A's one failure is its whole
    history: a test to watch, but not flaky."""
    agg = hs.aggregate(RUNS, 1)
    assert [w['nodeid'] for w in agg['watch']] == [A]
    facts = agg['watch'][0]['facts']
    assert (facts['runs'], facts['rate'], facts['flaky']) == (1, 1.0, False)
    assert {m['module']: m['runs'] for m in agg['modules']} == {'views': 1, 'media': 1}
    assert {m['module']: m['watch'] for m in agg['modules']} == {'views': 1, 'media': 0}


def test_health_verdict_one_line_per_module():
    """One exact sentence per module; none reads 'No archived runs yet.'"""
    assert hs.health_verdict(hs.aggregate(RUNS, 20)) == [
        'Media: 100% pass rate over 1 run, 0 tests to watch.',
        'Views: 75% pass rate over 4 runs, 1 test to watch.']
    assert hs.health_verdict(hs.aggregate([], 20)) == ['No archived runs yet.']
    odd = hs.aggregate([{'name': '2026-08-01_10-00-00__x', 'summary': {}}], 20)
    assert hs.health_verdict(odd) == ['All tests: no results over 1 run, 0 tests to watch.']
    # Failures in runs without per-test records cannot name a test: say
    # since when the records exist, never "0 tests to watch".
    old = hs.aggregate([_run('2026-08-01_10-00-00__views', {'passed': 3, 'failed': 1}),
                        _run('2026-08-02_10-00-00__views', {'passed': 1},
                             tests=[_t(A, 'passed', 1)])], 20)
    assert hs.health_verdict(old) == [
        'Views: 80% pass rate over 2 runs, no per-test history before Aug 2, 10:00 AM.']
    assert old['window']['history_since'] == '2026-08-02T10-00-00'
    none = hs.aggregate([_run('2026-08-01_10-00-00__views', {'passed': 3, 'failed': 1})], 20)
    assert hs.health_verdict(none) == [
        'Views: 75% pass rate over 1 run, no per-test history yet.']
    assert none['window']['history_since'] is None
    for text in hs.health_verdict(old) + hs.health_verdict(none):
        assert 'unknown' not in text


def test_aggregate_tolerates_nothing_and_odd_summaries():
    """Aggregation returns empty results for no runs and None rates for malformed summaries."""
    empty = hs.aggregate([], 20)
    assert empty['modules'] == []
    assert empty['groups'] == []
    assert empty['window'] == {'last_n': 20, 'runs': 0, 'newest_started': None,
                               'oldest_started': None, 'history_since': None,
                               'unrecorded_failing_runs': 0}
    odd = hs.aggregate([{'name': '2026-08-01_10-00-00__x', 'summary': {'counts': 'no',
                        'tests': 'no', 'advisories': 'no', 'target': 'no'}}], 20)
    assert odd['modules'][0]['pass_rate'] is None
    assert odd['modules'][0]['mean_full_run_s'] is None
    assert odd['modules'][0]['name'] == 'All tests'
    assert odd['modules'][0]['run_list'][0]['scope'] == 'all tests'


def test_intro_lines_use_friendly_dates_and_no_folder_names():
    """The intro says the window, its span, the pass rate rule, and the
    build, in words. No run folder name, anywhere on the page but in an href."""
    agg = hs.aggregate(RUNS, 20)
    assert hs.intro_lines(agg, '2026-08-24T16:01:01') == [
        'The newest 20 runs of each module, from the run archive.',
        '5 runs, Aug 21 to Aug 24, 2026.',
        'Pass rate = passed / (passed + failed + errors); skipped tests do not count.',
        'Built Aug 24, 4:01 PM. Rebuild: python run.py health']
    assert agg['window']['oldest_started'] == '2026-08-21T12-00-00'
    assert agg['window']['newest_started'] == '2026-08-24T12-00-00'
    one_day = hs.aggregate([RUNS[0], RUNS[4]], 20)
    assert hs.intro_lines(one_day, 'x')[1] == '2 runs on Aug 24, 2026.'
    assert hs.intro_lines(hs.aggregate([RUNS[0]], 20), 'x')[1] == '1 run on Aug 24, 2026.'
    years = hs.aggregate([RUNS[0], _run('2025-12-30_08-00-00__views', {'passed': 1})], 20)
    assert hs.intro_lines(years, 'x')[1] == '2 runs, Dec 30, 2025 to Aug 24, 2026.'
    assert len(hs.intro_lines(hs.aggregate([], 20), 'x')) == 3  # no span without runs
    page = hs.render_health(agg, generated='2026-08-24T16:01:01')
    assert ('<p class="case">The newest 20 runs of each module, from the run archive.</p>'
            '<p class="case">5 runs, Aug 21 to Aug 24, 2026.</p>') in page
    assert 'Built Aug 24, 4:01 PM. Rebuild: <code>python run.py health</code></p>' in page
    visible = re.sub(r'href="[^"]*"', '', page)
    assert not RUN_NAME_RE.search(visible)
    assert 'Generated' not in page
    assert 'Window:' not in page


def test_render_health_has_every_section_and_run_links(monkeypatch):
    """The health page shows every section, links each run and handles an empty archive."""
    monkeypatch.delenv('QATOUCH_PROJECT_ID', raising=False)
    page = hs.render_health(hs.aggregate(RUNS, 20), generated='2026-08-24T15:00:00')
    for heading in ('Products (1)', 'Modules (2)', 'Tests to watch (1 test in 1 module)'):
        assert heading in page
    for gone in ('Flaky candidates', 'Newly failing', 'Slowest tests', 'Test health', 'exit 1',
                 'Advisories', 'Targets', 'unknown'):
        assert gone not in page
    assert '<title>Test Health</title>' in page
    assert '<h1>Test Health</h1>' in page
    # The tiles lead the page. The modules follow, grouped under their
    # product, each group opening with one verdict line per module.
    assert (page.index('id="products"') < page.index('id="modules"')
            < page.index('<h3 class="product-group">NexaiQ-Core (2 modules)</h3>')
            < page.index('<ul class="verdict">'))
    assert '<li>Views: 75% pass rate over 4 runs, 1 test to watch.</li>' in page
    assert '<td class="num">75%</td>' in page
    assert '<td class="num">6 / 2</td>' in page
    # The headers say what they count. The newest run's outcome is a word.
    assert f'<th title="{hs.NOTES_TITLE}">Notes</th>' in page
    assert f'<th title="{hs.FULL_RUN_TITLE}">Mean full run</th>' in page
    assert '<div class="case"><span title="exit code 1">Failed</span></div>' in page
    # The advisory count links the newest run's advisories; a zero count does not link.
    assert '<a href="2026-08-24_12-00-00__views/run.html#advisories">1</a>' in page
    assert '#advisories">0</a>' not in page
    # The watch table: strip blocks linked to their runs, the rate, the last failure.
    watch = page[page.index('id="watch"'):]
    assert ('<a class="blk failed" href="2026-08-24_12-00-00__views/run.html" '
            'title="Aug 24, 12:00 PM: Failed"></a>') in watch
    assert '33% (1 of 3 failed) <span class="chip delta flaky">Flaky</span>' in watch
    assert '<a href="2026-08-24_12-00-00__views/run.html">Aug 24, 12:00 PM</a>' in watch
    # the module is a block that opens on a click, and it says what it holds
    assert '<details class="watch-module"><summary><span class="name">Views</span>' in watch
    assert '1 test, worst 33%' in watch
    assert 'passed 2/3' not in watch  # the strip text is the rate column's job here
    assert 'prefers-color-scheme: dark' in page
    assert 'href="index.html"' in page
    empty = hs.render_health(hs.aggregate([], 20))
    assert 'No archived runs yet' in empty
    assert 'None: no test failed in the window.' in empty
    # Failures in record-less runs: the empty table says why it names nothing.
    old = hs.render_health(hs.aggregate(
        [_run('2026-08-01_10-00-00__old', {'passed': 3, 'failed': 1})], 20))
    assert ('No test named: the failures in this window are in 1 run archived without '
            'per-test records.') in old
    assert 'None: no test failed' not in old


def test_module_rows_show_friendly_newest_run_and_expandable_run_list():
    """The newest run cell is the date and the outcome word, linked. The
    run list sits behind a toggle: date (linked), what ran, outcome."""
    page = hs.render_health(hs.aggregate(RUNS, 20), generated='x')
    assert ('<td><a href="2026-08-24_12-00-00__views/run.html">Aug 24, 12:00 PM</a>'
            '<div class="case"><span title="exit code 1">Failed</span></div></td>') in page
    assert ('<td>Views<details class="runs"><summary>Show runs</summary><ul>'
            '<li><a href="2026-08-24_12-00-00__views/run.html">Aug 24, 12:00 PM</a>, '
            'whole module: <span title="exit code 1">Failed</span></li>'
            '<li><a href="2026-08-23_12-00-00__views/run.html">Aug 23, 12:00 PM</a>, '
            'whole module: <span title="exit code 0">Passed</span></li>'
            '<li><a href="2026-08-22_12-00-00__views-test_a.py-test_one/run.html">'
            'Aug 22, 12:00 PM</a>, 1 test: Open a view: <span title="exit code 0">Passed</span></li>'
            '<li><a href="2026-08-21_12-00-00__views-test_a.py-test_two/run.html">'
            'Aug 21, 12:00 PM</a>, 1 test: Rename a view: '
            '<span title="exit code 1">Failed</span></li></ul></details></td>') in page
    assert page.count('<details class="runs">') == 2
    assert 'test_a.py::' not in page.split('id="watch"')[0]  # no node id before the watch table


def test_format_table_is_compact(monkeypatch):
    """The terminal view: the window, one line per product, the modules
    under each product, the module table, and the tests to watch."""
    monkeypatch.delenv('QATOUCH_PROJECT_ID', raising=False)
    text = hs.format_table(hs.aggregate(RUNS, 20))
    lines = text.splitlines()
    assert lines[0] == 'Health: newest 20 runs of each module. 5 runs, Aug 21 to Aug 24, 2026.'
    assert lines[1] == ('NexaiQ-Core: 78% pass rate over 5 runs, 1 test to watch, 2 modules, '
                        'last run Aug 24, 12:00 PM.')
    assert lines[2:6] == [
        '', 'NexaiQ-Core (2 modules)',
        '  Media: 100% pass rate over 1 run, 0 tests to watch.',
        '  Views: 75% pass rate over 4 runs, 1 test to watch.']
    views = next(line for line in lines if line.startswith('Views '))
    assert '75%' in views
    assert '1 min 40 s' in views
    assert views.endswith('Aug 24, 12:00 PM Failed')
    assert 'exit 1' not in text
    assert 'NOTES' in text
    assert 'Tests to watch (1):\n   33%  1/3 failed  last Aug 24, 12:00 PM  Open a view (Views)' in text
    assert '::' not in text
    assert not RUN_NAME_RE.search(text)
    assert 'Slowest' not in text
    assert 'TARGET' not in text


def test_product_shows_per_row_only_when_a_module_is_mixed(monkeypatch):
    """The heading names the product. A row repeats it only when the
    module's runs span more than one product."""
    monkeypatch.delenv('QATOUCH_PROJECT_ID', raising=False)
    runs = [
        _run('2026-08-24_12-00-00__views', {'passed': 2}, tests=[_t(A, 'passed', 1)]),
        _run('2026-08-23_12-00-00__views', {'passed': 2}, tests=[_t(A, 'passed', 1)]),
        _run('2026-08-24_09-00-00__media', {'passed': 4}, target=['tests/ui/media']),
    ]
    runs[0]['summary']['provenance'] = {'env': 'qa', 'product': 'NexaiQ-Core'}
    runs[1]['summary']['provenance'] = {'env': 'qa', 'product': 'NexaiQ-Camera 2 Cloud'}
    agg = hs.aggregate(runs, 20)
    assert {m['module']: m['products'] for m in agg['modules']} == {
        'views': ['NexaiQ-Core', 'NexaiQ-Camera 2 Cloud'], 'media': ['NexaiQ-Core']}
    assert [(g['product'], [m['module'] for m in g['modules']]) for g in agg['groups']] == [
        ('NexaiQ-Core', ['media', 'views'])]
    page = hs.render_health(agg, generated='2026-08-24T15:00:00')
    assert page.count('<span class="product">') == 1
    assert '<td>Views <span class="product">NexaiQ-Core, NexaiQ-Camera 2 Cloud</span><details' in page
    assert '<li>Views: 100% pass rate over 2 runs, 0 tests to watch.</li>' in page
    text = hs.format_table(agg)
    assert ('  Views: 100% pass rate over 2 runs, 0 tests to watch. '
            'Products: NexaiQ-Core, NexaiQ-Camera 2 Cloud.') in text
    assert '  Media: 100% pass rate over 1 run, 0 tests to watch.\n' in text
    assert 'Product:' not in text


C2C = 'tests/ui/c2c/test_c.py::test_c'
TITLES[C2C] = 'Open a camera'


def _c2c_runs() -> list[dict]:
    """RUNS plus two Camera 2 Cloud runs of one module, one of them failing,
    and a whole-suite run without records, so the tiles have three products."""
    c2c = [
        _run('2026-08-24_11-00-00__c2c', {'passed': 1, 'failed': 1},
             tests=[_t(C2C, 'failed', 3, 'ui/c2c/test_c.py')], exitstatus=1, duration=10,
             target=['tests/ui/c2c']),
        _run('2026-08-23_11-00-00__c2c', {'passed': 2},
             tests=[_t(C2C, 'passed', 3, 'ui/c2c/test_c.py')], duration=10,
             target=['tests/ui/c2c']),
        _run('2026-08-20_11-00-00__all', {'passed': 5, 'failed': 1}, target=['tests']),
    ]
    c2c[2]['summary']['provenance'] = {'product': 'Mixed: NexaiQ-Camera 2 Cloud, NexaiQ-Core'}
    return [*RUNS, *c2c]


def test_product_tiles_aggregate_the_runs_of_each_product(monkeypatch):
    """One tile per product: runs in the window, pass rate over those
    runs, tests to watch that failed in them, modules, and the newest
    run linked. Named products first in name order, then the mixed labels."""
    monkeypatch.delenv('QATOUCH_PROJECT_ID', raising=False)
    agg = hs.aggregate(_c2c_runs(), 20)
    assert [p['product'] for p in agg['products']] == [
        'NexaiQ-Camera 2 Cloud', 'NexaiQ-Core', 'Mixed: NexaiQ-Camera 2 Cloud, NexaiQ-Core']
    camera, core, mixed = agg['products']
    assert camera == {'product': 'NexaiQ-Camera 2 Cloud', 'runs': 2, 'modules': 1, 'passed': 3,
                      'failed': 1, 'pass_rate': 0.75, 'watch': 1,
                      'newest_run': '2026-08-24_11-00-00__c2c',
                      'newest_started': '2026-08-24T11-00-00'}
    assert (core['runs'], core['modules'], core['passed'], core['failed']) == (5, 2, 7, 2)
    assert (core['pass_rate'], core['watch']) == (0.778, 1)
    assert core['newest_run'] == '2026-08-24_12-00-00__views'
    assert (mixed['runs'], mixed['modules'], mixed['pass_rate'], mixed['watch']) == (1, 1, 0.833, 0)
    page = hs.render_health(agg, generated='x')
    tile = page[page.index('<div class="tile"><div class="tile-name">NexaiQ-Camera 2 Cloud</div>'):]
    tile = tile[:tile.index('</div></div>')]
    for fact in ('<span class="k">Runs in window</span><span class="v">2</span>',
                 '<span class="k">Pass rate</span><span class="v">75%</span>',
                 '<span class="k">Tests to watch</span><span class="v">1</span>',
                 '<span class="k">Modules</span><span class="v">1</span>',
                 ('<span class="k">Last run</span><span class="v">'
                 '<a href="2026-08-24_11-00-00__c2c/run.html">Aug 24, 11:00 AM</a></span>')):
        assert fact in tile
    assert (page.index('tile-name">NexaiQ-Camera 2 Cloud') < page.index('tile-name">NexaiQ-Core')
            < page.index('tile-name">Mixed:'))
    assert ('NexaiQ-Camera 2 Cloud: 75% pass rate over 2 runs, 1 test to watch, 1 module, '
            'last run Aug 24, 11:00 AM.') in hs.format_table(agg)


def test_modules_group_under_their_product_in_tile_order(monkeypatch):
    """Modules sit under a heading per product, groups in the tiles'
    order and modules in name order inside each. The page renders the
    headings in that order with each module's row under its own, and
    the whole-suite run reads as "All tests"."""
    monkeypatch.delenv('QATOUCH_PROJECT_ID', raising=False)
    agg = hs.aggregate(_c2c_runs(), 20)
    assert [(g['product'], [m['module'] for m in g['modules']]) for g in agg['groups']] == [
        ('NexaiQ-Camera 2 Cloud', ['c2c']), ('NexaiQ-Core', ['media', 'views']),
        ('Mixed: NexaiQ-Camera 2 Cloud, NexaiQ-Core', [''])]
    assert [m['name'] for m in agg['modules']] == ['C2c', 'Media', 'Views', 'All tests']
    page = hs.render_health(agg, generated='x')
    heads = re.findall(r'<h3 class="product-group">([^<]+)</h3>', page)
    assert heads == ['NexaiQ-Camera 2 Cloud (1 module)', 'NexaiQ-Core (2 modules)',
                     'Mixed: NexaiQ-Camera 2 Cloud, NexaiQ-Core (1 module)']
    assert (page.index(heads[0]) < page.index('<td>C2c<details')
            < page.index(heads[1]) < page.index('<td>Media<details')
            < page.index('<td>Views<details')
            < page.index(heads[2]) < page.index('<td>All tests<details'))
    assert 'all tests: <span title="exit code 0">Passed</span>' in page


def test_every_module_names_a_product(monkeypatch):
    """A run archived without per-test records is named by its targets
    (the c2c folder, else the mapping default), so no module, verdict
    line, or tile is blank."""
    monkeypatch.delenv('QATOUCH_PROJECT_ID', raising=False)
    runs = [
        _run('2026-08-24_10-00-00__c2c', {'passed': 1}, target=['tests/ui/c2c']),
        _run('2026-08-24_09-00-00__media', {'passed': 1}, target=['tests/ui/media']),
        {'name': '2026-08-24_08-00-00__bare', 'summary': {}},
    ]
    agg = hs.aggregate(runs, 20, default_key='K5kB')
    assert {m['module']: m['product'] for m in agg['modules']} == {
        'c2c': 'NexaiQ-Camera 2 Cloud', 'media': 'NexiaQ-Mobile', '': 'NexiaQ-Mobile'}
    assert [p['product'] for p in agg['products']] == ['NexaiQ-Camera 2 Cloud', 'NexiaQ-Mobile']
    assert all(m['product'] for m in hs.aggregate(runs, 20)['modules'])
    page = hs.render_health(agg, generated='x')
    assert '<span class="product">' not in page  # no module spans two products
    assert page.count('<li>') == 6  # a verdict line and a run-list line per module
    assert 'NexiaQ-Mobile (2 modules)' in hs.format_table(agg)


def test_run_links_open_what_each_folder_has(tmp_path):
    """With the archive on disk, a run without run.html links its
    report.html, and one with neither links its folder."""
    runs = tmp_path / 'runs'
    for run in RUNS:
        (runs / run['name']).mkdir(parents=True)
    (runs / '2026-08-24_12-00-00__views' / 'report.html').write_text('x', encoding='utf-8')
    (runs / '2026-08-22_12-00-00__views-test_a.py-test_one' / 'run.html').write_text(
        'x', encoding='utf-8')
    page = hs.render_health(hs.aggregate(RUNS, 20), generated='x', runs_dir=runs)
    assert 'href="2026-08-24_12-00-00__views/report.html#advisories"' in page
    assert '<a href="2026-08-24_12-00-00__views/report.html">Aug 24, 12:00 PM</a>' in page
    assert 'class="blk failed" href="2026-08-24_12-00-00__views/report.html"' in page
    assert '<a href="2026-08-22_12-00-00__views-test_a.py-test_one/run.html">Aug 22, 12:00 PM</a>' \
        in page
    assert 'href="2026-08-23_12-00-00__views/"' in page
    assert 'href="2026-08-24_12-00-00__views/run.html"' not in page


def test_cli_writes_health_html_and_links_it_from_the_index(tmp_path, capsys):
    """The CLI writes health.html into the archive and links it from index.html."""
    runs = tmp_path / 'runs'
    for run in RUNS:
        folder = runs / run['name']
        folder.mkdir(parents=True)
        (folder / 'summary.json').write_text(json.dumps(run['summary']), encoding='utf-8')
    (runs / 'index.html-not-a-run').mkdir()
    assert hs.main(['--runs-dir', str(runs), '--last', '3']) == 0
    out = capsys.readouterr().out
    assert 'Health: newest 3 runs of each module. 4 runs, Aug 22 to Aug 24, 2026.' in out
    assert str(runs / 'health.html') in out
    assert (runs / 'health.html').is_file()
    assert 'href="health.html"' in (runs / 'index.html').read_text(encoding='utf-8')


def test_cli_without_runs_writes_nothing(tmp_path, capsys):
    """The CLI writes no health page when the archive has no runs."""
    assert hs.main(['--runs-dir', str(tmp_path / 'none')]) == 0
    assert 'No archived runs' in capsys.readouterr().out
    assert not (tmp_path / 'none' / 'health.html').exists()


def _row(nodeid: str, outcome: str, module: str = 'ui/views/test_a.py', title: str = 'A title') -> dict:
    return {'nodeid': nodeid, 'module': module, 'title': title, 'outcome': outcome, 'duration_s': 5}


def test_a_test_with_an_xdist_group_suffix_is_the_same_test():
    """'test_one[chromium]' and 'test_one[chromium]@account_views' are 1 history, so the watch list names it 1 time."""
    grouped = A + '@account_views'
    runs = [_run('2026-09-21_09-00-00__views', {'failed': 1}, tests=[_row(grouped, 'failed')], exitstatus=1),
            _run('2026-09-17_09-00-00__views', {'passed': 1}, tests=[_row(A, 'passed')])]
    agg = hs.aggregate(runs, 20)
    assert [w['nodeid'] for w in agg['watch']] == [A]
    assert agg['watch'][0]['facts']['failed'] == 1
    assert len(agg['watch'][0]['facts']['blocks']) == 2


def test_a_parametrized_test_is_one_row_and_its_variants_fold_below_it():
    """10 cameras that fail 1 test are 1 row ('9 of 10 variants failed'), not 9 rows; modules come worst first."""
    base = 'tests/c2c/test_c2c_video.py::TestVideo::test_codec'
    cams = [f'{base}[chromium-cam-{n}]' for n in range(10)]
    newest = [_row(c, 'passed' if n == 0 else 'failed', 'c2c/test_c2c_video.py', 'Codec') for n, c in enumerate(cams)]
    older = [_row(c, 'passed', 'c2c/test_c2c_video.py', 'Codec') for c in cams]
    runs = [_run('2026-09-21_10-00-00__c2c', {'passed': 1, 'failed': 9}, tests=newest, exitstatus=1, target=['tests/c2c']),
            _run('2026-09-20_10-00-00__c2c', {'passed': 10}, tests=older, target=['tests/c2c']),
            RUNS[0], RUNS[1]]
    agg = hs.aggregate(runs, 20)
    assert len(agg['watch']) == 10          # the flat list still holds every variant
    groups = agg['watch_groups']
    assert [len(m['rows']) for m in groups] == [1, 1]
    folded = next(r for m in groups for r in m['rows'] if r['base'] == base)
    assert (len(folded['variants']), folded['variant_total']) == (9, 10)
    page = hs.render_health(agg, generated='2026-09-21T12:00:00')
    watch = page[page.index('id="watch"'):]
    assert 'Tests to watch (2 tests in 2 modules)' in page
    assert '9 of 10 variants failed: show them' in watch
    assert '<code>chromium-cam-3</code>' in watch
    assert watch.count('<details class="watch-module">') == 2
    assert 'Open all' in watch


def _labelled(name: str, box: str, tests: list, failed: int = 0) -> dict:
    """A run archived by an older version, whose provenance carries a tenant label."""
    run = _run(name, {'passed': len(tests) - failed, 'failed': failed}, tests=tests, exitstatus=1 if failed else 0)
    run['summary']['provenance'] = {'box': box}
    return run


def test_an_older_runs_tenant_label_is_pooled_and_the_page_has_no_by_box_column():
    """One server: the page once split each module's rate by tenant, and with one
    tenant the column was always blank and read as a bug. Runs archived with a
    `box` label still count; the label itself changes nothing on the page."""
    runs = [_labelled('2026-09-21_10-00-00__views', 'b', [_row(A, 'failed')], failed=1),
            _labelled('2026-09-21_09-00-00__views', 'a', [_row(A, 'passed')]),
            _labelled('2026-09-20_10-00-00__views', 'b', [_row(A, 'failed')], failed=1),
            _labelled('2026-09-20_09-00-00__views', 'a', [_row(A, 'passed')])]
    agg = hs.aggregate(runs, 20)
    views = next(m for m in agg['modules'] if m['module'] == 'views')
    assert (views['passed'], views['failed'], views['pass_rate']) == (2, 2, 0.5)
    assert 'by_box' not in views and 'boxes' not in agg['watch'][0]
    page = hs.render_health(agg, generated='2026-09-21T12:00:00')
    assert 'By box' not in page and 'by-box' not in page
    assert not hasattr(hs, 'boxes_line') and not hasattr(hs, 'box_facts')

