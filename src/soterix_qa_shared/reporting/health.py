"""Health summary across archived runs -- CLI.

    python run.py health                     # newest 20 runs of each module
    python run.py health 50                  # newest 50
    python tools/health_summary.py --last 50

Reads every reports/runs/*/summary.json (see plugins/run_archive.py),
writes reports/runs/health.html, and prints a compact table. The page
shows, over the window:
  - Product tiles: one per product, with its runs in the window, its
    pass rate, its tests to watch, its modules, and its last run (linked).
  - Modules, grouped under a product heading. A module is the folder
    under tests/ (tests/ui/views is "Views", tests/tools is "Tools").
    Every run whose per-test records or targets fall in a module counts
    toward it, so a run of one test pools into its module instead of
    standing as its own row. Each group opens with one verdict line per
    module in plain words, e.g. "Views: 96% pass rate over 20 runs, 1
    test to watch.", then a table: one row per module with its runs,
    pass rate over test outcomes, passed and failed counts, mean
    duration of a full-module run, notes count (the advisories, linked
    to the newest run's lines), and the newest run with its outcome as
    a word. Each row expands to list its runs: the date, what was run
    in words ("whole module", "marker: dragdrop", "1 test: Export
    bookmark"), and the outcome, each linked to the run.
  - Tests to watch: every test that failed at least once in the window,
    with its last 20 outcomes as a strip (oldest left), its flaky rate
    (failures / runs it was present in), the flaky label when it also
    passed in that window, and the date of its last failure. Sorted by
    flaky rate, highest first.

Pass rate is passed / (passed + failed + errors); skips do not count. The
window is the newest N runs of each module (default 20), so a module that
runs rarely still has a row.

Summaries written before schema 2 carry no per-test records. Such a run
counts its totals toward each module its targets name (one, nearly
always), and its failures name no test, so the module's verdict reads
"no per-test history before <date>" in place of "0 tests to watch". A
run's product is the one it recorded (schema 4), else the one its
per-test records' node ids derive, else the one its invocation targets
name (plugins/products.target_product), so no row or tile is ever
blank. The page prints no run folder name and no node id as a name:
a run shows as its date, a test as its recorded title or its function
name as words. Every link to a run goes through run_report.run_href, so
a run archived without a run.html opens its report.html or its folder
instead of a missing file. Stdlib only, and read-only on the archive
except for health.html and the index link to it.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Mapping
from datetime import datetime
from html import escape
from pathlib import Path

from soterix_qa_shared.reporting import live as live_record
from soterix_qa_shared.reporting import run_archive
from soterix_qa_shared.reporting.branding import favicon_link, header_logo
from soterix_qa_shared.reporting.products import is_mixed, target_product
from soterix_qa_shared.reporting.run_archive import (
    _write_index,
    load_project_key,
)
from soterix_qa_shared.reporting.run_report import (
    FAILURE_OUTCOMES,
    HISTORY_RUNS,
    NOTES_TITLE,
    PAGE_CSS,
    PASSED_OUTCOMES,
    RUN_DIR_RE,
    RunLinks,
    exit_outcome,
    format_seconds,
    friendly_date,
    friendly_time,
    history_facts,
    module_of,
    outcome_html,
    parse_started,
    plural,
    readable_title,
    render_strip,
    run_href,
    run_started,
    summary_product,
    summary_tests,
)

DEFAULT_LAST = 20
# The terminal table stays readable at 100 columns.
NAME_WIDTH = 20
REBUILD_COMMAND = 'python run.py health'
FULL_RUN_TITLE = 'Mean duration of the runs that ran the whole module folder, with no marker or keyword'


# ---------------------------------------------------------------- load

def load_runs(runs_dir: Path) -> list[dict]:
    """Every run folder with a readable summary.json, newest first."""
    runs = []
    if not runs_dir.is_dir():
        return runs
    for folder in runs_dir.iterdir():
        if not folder.is_dir() or not RUN_DIR_RE.match(folder.name):
            continue
        try:
            loaded = json.loads((folder / 'summary.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if isinstance(loaded, dict):
            runs.append({'name': folder.name, 'summary': loaded})
    return sorted(runs, key=lambda r: r['name'], reverse=True)


# ---------------------------------------------------------------- modules

def _parts(path: str) -> list[str]:
    """A path's parts under tests/, with the ui/ namespace dropped."""
    parts = [p for p in str(path or '').split('::', 1)[0].replace('\\', '/').split('/') if p]
    if parts[:1] == ['tests']:
        parts = parts[1:]
    if parts[:1] == ['ui']:
        parts = parts[1:]
    return parts


def module_key(path: str) -> str:
    """The module a test path or invocation target falls in: the folder
    under tests/. 'tests/ui/views/x.py::t' and 'ui/views/x.py' (the form
    module_of gives) are both 'views'; 'tests/tools' is 'tools'. The
    tests root, a bare file there, or no path at all is '' (the whole
    suite). A path outside tests/ keeps its first folder."""
    parts = _parts(path)
    if not parts or parts[0].endswith('.py'):
        return ''
    return parts[0]


def module_name(key: str) -> str:
    """'views' reads 'Views', 'lpr_search' reads 'Lpr search', and ''
    (the whole suite) reads 'All tests'."""
    if not key:
        return 'All tests'
    words = key.replace('_', ' ')
    return words[:1].upper() + words[1:]


def _targets(summary: Mapping) -> list[str]:
    targets = summary.get('target')
    return [str(t) for t in targets if str(t or '').strip()] if isinstance(targets, list) else []


def run_modules(summary: Mapping) -> list[str]:
    """The modules a run touched, each once, in first-seen order: those
    of its per-test records, else those its targets name. Never empty:
    a run without either ran the whole suite."""
    keys = [module_key(str(r['nodeid'])) for r in summary_tests(summary)]
    if not keys:
        keys = [module_key(t) for t in _targets(summary)] or ['']
    return list(dict.fromkeys(keys))


def _is_full_module_run(summary: Mapping, key: str) -> bool:
    """One target, the module's own folder, no marker or keyword: a run
    whose duration is the module's."""
    targets = _targets(summary)
    if len(targets) != 1 or summary.get('markexpr') or summary.get('keyword'):
        return False
    return _parts(targets[0]) == ([key] if key else [])


def run_scope(summary: Mapping, titles: Mapping[str, str] | None = None) -> str:
    """What a run ran, in words: 'whole module' for a module folder,
    'marker: dragdrop' for that folder with a marker, '1 test: Export
    bookmark' for a node id (its recorded title, else its function name
    as words), '1 file: test_admin_sites.py' for a file, and 'all tests'
    for the suite. Several targets join: '2 tests: A + B', or 'all of
    Media + all of Maps'. A marker or keyword on anything else is
    appended: '1 file: x.py, marker: smoke'. `titles` maps node ids to
    their recorded titles."""
    titles = titles or {}
    targets = _targets(summary)
    tests = [t for t in targets if '::' in t]
    files = [t for t in targets if '::' not in t and t.rstrip('/').endswith('.py')]
    folders = [t for t in targets if t not in tests and t not in files]
    parts = []
    if tests:
        # A docstring title ends in a period; the outcome follows it here.
        names = ' + '.join((titles.get(t) or readable_title(t)).rstrip('.') for t in tests)
        parts.append(f"{plural(len(tests), 'test', 'tests')}: {names}")
    if files:
        names = ' + '.join(f.replace('\\', '/').rstrip('/').rsplit('/', 1)[-1] for f in files)
        parts.append(f"{plural(len(files), 'file', 'files')}: {names}")
    wholes = []
    for folder in folders:
        path = _parts(folder)
        if len(path) > 1:
            parts.append(f'folder: {path[-1]}')
        elif len(targets) == 1:
            parts.append('whole module' if path else 'all tests')
        else:
            wholes.append(f'all of {module_name(path[0] if path else "")}')
    if wholes:
        parts.append(' + '.join(wholes))
    text = ', '.join(parts) or 'all tests'
    for label, value in (('marker', summary.get('markexpr')), ('keyword', summary.get('keyword'))):
        value = str(value or '').strip()
        if value:
            text = f'{label}: {value}' if text in ('whole module', 'all tests') else f'{text}, {label}: {value}'
    return text


def select_window(runs: Iterable[dict], last_n: int = DEFAULT_LAST) -> list[dict]:
    """The newest `last_n` runs of each module, newest first overall. A
    run stays when a module it touched still has room."""
    seen: dict[str, int] = {}
    window = []
    for run in sorted(runs, key=lambda r: r['name'], reverse=True):
        keep = False
        for key in run_modules(run['summary']):
            if seen.get(key, 0) < last_n:
                seen[key] = seen.get(key, 0) + 1
                keep = True
        if keep:
            window.append(run)
    return window


# ---------------------------------------------------------------- aggregate

def _rate(passed: int, failed: int) -> float | None:
    total = passed + failed
    return round(passed / total, 3) if total else None


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 1) if values else None


def aggregate(runs: Iterable[dict], last_n: int = DEFAULT_LAST,
              default_key: str | None = None) -> dict:
    """Fold the window's summaries into the page's facts. Pure.

    `default_key` is the mapping file's project_key, the product a
    target names when neither the run nor its targets name one.
    """
    window = select_window(runs, last_n)
    modules: dict[str, dict] = {}
    # product label -> its tile facts
    products: dict[str, dict] = {}
    # run name -> its product label, for the per-product watch counts
    run_products: dict[str, str] = {}
    # nodeid -> its results, newest first:
    # {'run', 'started', 'outcome', 'seconds', 'title', 'module'}
    history: dict[str, list[dict]] = {}
    # nodeid -> its newest recorded title, so a run of one test that
    # recorded none can still be named in words
    titles: dict[str, str] = {}
    # The window is newest first, so the last run seen with records is
    # the oldest: per-test history starts there.
    history_since = None
    unrecorded_failing_runs = 0

    for run in window:
        name, summary = run['name'], run['summary']
        counts = summary.get('counts') if isinstance(summary.get('counts'), dict) else {}
        advisories = summary.get('advisories') if isinstance(summary.get('advisories'), list) else []
        started = run_started(Path(name), summary)
        passed = int(counts.get('passed', 0) or 0)
        failed = int(counts.get('failed', 0) or 0) + int(counts.get('errors', 0) or 0)
        rows = summary_tests(summary)
        keys = run_modules(summary)
        # A run that recorded no product is named by its targets, never blank.
        product = (summary_product(summary)
                   or target_product(_targets(summary), default_key))
        run_products[name] = product
        # The window is newest first, so the first run seen is the tile's newest.
        tile = products.setdefault(product, {
            'product': product, 'runs': 0, 'passed': 0, 'failed': 0, 'modules': set(),
            'newest_run': name, 'newest_started': started})
        tile['runs'] += 1
        tile['passed'] += passed
        tile['failed'] += failed
        tile['modules'].update(keys)
        if rows:
            history_since = started
        elif failed:
            unrecorded_failing_runs += 1

        # An advisory counts toward the module of the test it names,
        # else toward every module of the run.
        notes: dict[str, int] = {}
        for adv in advisories:
            test = str(adv.get('test') or '') if isinstance(adv, dict) else ''
            for key in ([module_key(test)] if test else keys):
                notes[key] = notes.get(key, 0) + 1

        for key in keys:
            mod = _module_row(modules, key, name, started, summary.get('exitstatus'))
            mod['runs'].append({'run': name, 'started': started, 'exit': summary.get('exitstatus'),
                                'records': bool(rows), 'summary': summary})
            if product not in mod['products']:
                mod['products'].append(product)
            mod['advisories'] += notes.get(key, 0)
            if _is_full_module_run(summary, key) and isinstance(summary.get('duration_s'), (int, float)):
                mod['full_durations'].append(float(summary['duration_s']))
            if not rows:
                # A pre-schema-2 archive: its totals are all it has, and
                # its failures can name no test.
                mod['passed'] += passed
                mod['failed'] += failed
                mod['unrecorded_failed'] += failed

        for row in rows:
            nodeid = str(row['nodeid'])
            module = str(row.get('module') or module_of(nodeid))
            outcome = str(row.get('outcome') or 'unknown')
            seconds = row['duration_s'] if isinstance(row.get('duration_s'), (int, float)) else None
            title = str(row.get('title') or readable_title(nodeid))
            titles.setdefault(nodeid, title)
            history.setdefault(nodeid, []).append({
                'run': name, 'started': started, 'outcome': outcome, 'seconds': seconds,
                'title': title, 'module': module})
            mod = modules[module_key(nodeid)]
            if outcome in PASSED_OUTCOMES:
                mod['passed'] += 1
            elif outcome in FAILURE_OUTCOMES:
                mod['failed'] += 1

    # Every test that failed at least once in its strip, with the facts
    # the strip shows, and how many of them each module and product saw.
    watch = []
    watch_by_module: dict[str, int] = {}
    watch_by_product: dict[str, int] = {}
    for nodeid, results in history.items():
        facts = history_facts(results)
        if not facts['failed']:
            continue
        newest = results[0]
        key = module_key(nodeid)
        watch.append({'nodeid': nodeid, 'title': newest['title'], 'module': newest['module'],
                      'module_name': module_name(key), 'facts': facts})
        watch_by_module[key] = watch_by_module.get(key, 0) + 1
        failing_runs = {b['run'] for b in facts['blocks'] if b['outcome'] in FAILURE_OUTCOMES}
        for product in {run_products[run] for run in failing_runs if run in run_products}:
            watch_by_product[product] = watch_by_product.get(product, 0) + 1
    watch.sort(key=lambda w: (-(w['facts']['rate'] or 0.0), -w['facts']['failed'], w['nodeid']))

    # Named products first, in name order, then the "Mixed:" labels.
    product_rows = []
    for tile in sorted(products.values(), key=lambda t: (is_mixed(t['product']), t['product'])):
        product_rows.append({
            'product': tile['product'], 'runs': tile['runs'], 'modules': len(tile['modules']),
            'passed': tile['passed'], 'failed': tile['failed'],
            'pass_rate': _rate(tile['passed'], tile['failed']),
            'watch': watch_by_product.get(tile['product'], 0),
            'newest_run': tile['newest_run'], 'newest_started': tile['newest_started'],
        })

    # Modules sit under the product of their newest run, groups in the
    # tiles' order, and within a group in name order.
    module_rows = []
    for mod in sorted(modules.values(),
                      key=lambda m: (is_mixed(m['products'][0]), m['products'][0], m['name'])):
        module_rows.append({
            'module': mod['module'], 'name': mod['name'], 'runs': len(mod['runs']),
            'passed': mod['passed'], 'failed': mod['failed'],
            'pass_rate': _rate(mod['passed'], mod['failed']),
            'mean_full_run_s': _mean(mod['full_durations']),
            'full_runs': len(mod['full_durations']),
            'advisories': mod['advisories'], 'watch': watch_by_module.get(mod['module'], 0),
            'unrecorded_failed': mod['unrecorded_failed'], 'history_since': history_since,
            'newest_run': mod['newest_run'], 'newest_started': mod['newest_started'],
            'newest_exit': mod['newest_exit'],
            'product': mod['products'][0], 'products': list(mod['products']),
            'mixed': len(mod['products']) > 1,
            'run_list': [{'run': r['run'], 'started': r['started'], 'exit': r['exit'],
                          'records': r['records'], 'scope': run_scope(r['summary'], titles)}
                         for r in mod['runs']],
        })
    groups: list[dict] = []
    for row in module_rows:
        if not groups or groups[-1]['product'] != row['product']:
            groups.append({'product': row['product'], 'modules': []})
        groups[-1]['modules'].append(row)

    return {
        'window': {'last_n': last_n, 'runs': len(window),
                   'newest_started': (run_started(Path(window[0]['name']), window[0]['summary'])
                                      if window else None),
                   'oldest_started': (run_started(Path(window[-1]['name']), window[-1]['summary'])
                                      if window else None),
                   'history_since': history_since,
                   'unrecorded_failing_runs': unrecorded_failing_runs},
        'products': product_rows, 'groups': groups, 'modules': module_rows, 'watch': watch,
    }


def _module_row(modules: dict[str, dict], key: str, name: str, started: str,
                exitstatus: object) -> dict:
    return modules.setdefault(key, {
        'module': key, 'name': module_name(key), 'runs': [], 'passed': 0, 'failed': 0,
        'unrecorded_failed': 0, 'full_durations': [], 'advisories': 0, 'products': [],
        'newest_run': name, 'newest_started': started, 'newest_exit': exitstatus})


def module_verdict(m: dict) -> str:
    """One module's sentence in plain words, numbers exact: "Views: 96%
    pass rate over 20 runs, 1 test to watch." A module whose runs
    recorded no results reads "no results" in place of the rate. A
    module that failed only in runs archived before per-test records
    existed cannot name its tests, and says since when it could."""
    rate = f"{_pct(m['pass_rate'])} pass rate" if m['pass_rate'] is not None else 'no results'
    if m['watch'] == 0 and m['unrecorded_failed']:
        watch = (f"no per-test history before {friendly_time(m['history_since'])}"
                 if m['history_since'] else 'no per-test history yet')
    else:
        watch = plural(m['watch'], 'test to watch', 'tests to watch')
    return f"{m['name']}: {rate} over {plural(m['runs'], 'run', 'runs')}, {watch}."


def health_verdict(agg: dict) -> list[str]:
    """One sentence per module, in the modules' (grouped) order."""
    return [module_verdict(m) for m in agg['modules']] or ['No archived runs yet.']


def product_verdict(p: dict) -> str:
    """One product's sentence: "NexaiQ-Core: 93% pass rate over 29 runs,
    2 tests to watch, 7 modules, last run Aug 24, 3:12 PM." """
    rate = f"{_pct(p['pass_rate'])} pass rate" if p['pass_rate'] is not None else 'no results'
    return (f"{p['product']}: {rate} over {plural(p['runs'], 'run', 'runs')}, "
            f"{plural(p['watch'], 'test to watch', 'tests to watch')}, "
            f"{plural(p['modules'], 'module', 'modules')}, "
            f"last run {friendly_time(p['newest_started'])}.")


def span_line(window: dict) -> str:
    """"30 runs, Aug 21 to Aug 24, 2026." with the year once, at the end.
    One day reads "3 runs on Aug 24, 2026."; two years name both."""
    count = plural(window['runs'], 'run', 'runs')
    oldest, newest = window['oldest_started'], window['newest_started']
    if friendly_date(oldest, True) == friendly_date(newest, True):
        return f'{count} on {friendly_date(newest, True)}.'
    first, last = parse_started(oldest), parse_started(newest)
    same_year = bool(first and last and first.year == last.year)
    return f'{count}, {friendly_date(oldest, not same_year)} to {friendly_date(newest, True)}.'


def intro_lines(agg: dict, generated: str) -> list[str]:
    """The page's opening lines, in words a reader outside the framework
    follows: the window, its span, the pass rate rule, and the build."""
    window = agg['window']
    lines = [f"The newest {window['last_n']} runs of each module, from the run archive."]
    if window['runs']:
        lines.append(span_line(window))
    lines.append('Pass rate = passed / (passed + failed + errors); skipped tests do not count.')
    lines.append(f'Built {friendly_time(generated)}. Rebuild: {REBUILD_COMMAND}')
    return lines


# ---------------------------------------------------------------- render

def _pct(rate: float | None) -> str:
    return '-' if rate is None else f'{rate * 100:.0f}%'


def _secs(seconds: float | None) -> str:
    return '-' if seconds is None else format_seconds(seconds)


def _run_link(name: str, started: str | None, runs_dir: Path | None = None) -> str:
    """A run as its date, linked. The folder name stays in the href."""
    return f'<a href="{run_href(name, runs_dir)}">{escape(friendly_time(started))}</a>'


def _product_chip(product: str) -> str:
    """The products beside a module that spans more than one."""
    return f' <span class="product">{escape(product)}</span>' if product else ''


def _tile(p: dict, runs_dir: Path | None = None) -> str:
    """One product's tile: its name, then runs, pass rate, tests to watch,
    modules, and the last run's date linked to that run."""
    facts = (('Runs in window', str(p['runs'])), ('Pass rate', _pct(p['pass_rate'])),
             ('Tests to watch', str(p['watch'])), ('Modules', str(p['modules'])),
             ('Last run', _run_link(p['newest_run'], p['newest_started'], runs_dir)))
    body = ''.join(f'<div class="tile-fact"><span class="k">{escape(k)}</span>'
                   f'<span class="v">{v}</span></div>' for k, v in facts)
    return f'<div class="tile"><div class="tile-name">{escape(p["product"])}</div>{body}</div>'


def _test_cell(row: dict) -> str:
    return f'{escape(row["title"])}<div class="nodeid">{escape(row["nodeid"])}</div>'


def _rate_cell(facts: dict) -> str:
    """'15% (3 of 20 failed)' plus the flaky label when the window also
    holds a pass. A test that never passed in the window is not flaky:
    it fails every time, and the label would hide that."""
    text = escape(f"{_pct(facts['rate'])} ({facts['failed']} of {facts['runs']} failed)")
    if facts['flaky']:
        text += ' <span class="chip delta flaky">Flaky</span>'
    return f'<td class="num">{text}</td>'


def _failure_link(block: dict | None, runs_dir: Path | None = None) -> str:
    if not block:
        return '-'
    return _run_link(block['run'], block.get('started'), runs_dir)


def _table(headers: list[str | tuple[str, str]], rows: list[list[str]], empty: str) -> str:
    """A header is its label, or (label, tooltip) for a column whose
    label alone does not say what it counts."""
    if not rows:
        return f'<p class="empty">{escape(empty)}</p>'
    head = ''.join(
        f'<th title="{escape(h[1], quote=True)}">{escape(h[0])}</th>' if isinstance(h, tuple)
        else f'<th>{escape(h)}</th>' for h in headers)
    body = ''.join('<tr>' + ''.join(cells) + '</tr>' for cells in rows)
    return f'<table class="facts-table"><tr>{head}</tr>{body}</table>'


def _num(text: str) -> str:
    return f'<td class="num">{escape(text)}</td>'


def _cell(inner: str) -> str:
    return f'<td>{inner}</td>'


def _module_cell(m: dict, runs_dir: Path | None) -> str:
    """The module's name, its products when it spans more than one, and
    the list of its runs behind a "Show runs" toggle: each as its date
    (linked), what it ran in words, and its outcome."""
    chip = _product_chip(', '.join(m['products'])) if m['mixed'] else ''
    items = ''.join(
        f'<li>{_run_link(r["run"], r["started"], runs_dir)}, {escape(r["scope"])}: '
        f'{outcome_html(r["exit"])}</li>' for r in m['run_list'])
    return _cell(f'{escape(m["name"])}{chip}<details class="runs"><summary>Show runs</summary>'
                 f'<ul>{items}</ul></details>')


def render_health(agg: dict, generated: str | None = None,
                  runs_dir: Path | None = None) -> str:
    """The health.html page from aggregate()'s facts. `runs_dir` is the
    archive on disk, for run_href to check which page each run has.
    Without it every run links its run.html."""
    window = agg['window']
    generated = generated or datetime.now().isoformat(timespec='seconds')  # noqa: DTZ005 - local-time display of a run timestamp; tz would shift the shown time

    def module_row(m: dict) -> list[str]:
        return [
            _module_cell(m, runs_dir),
            _num(str(m['runs'])), _num(_pct(m['pass_rate'])),
            _num(f"{m['passed']} / {m['failed']}"), _num(_secs(m['mean_full_run_s'])),
            # The count spans the window. The link opens the newest run's lines.
            _num(str(m['advisories'])) if not m['advisories'] else
            f'<td class="num"><a href="{run_href(m["newest_run"], runs_dir)}#advisories">'
            f'{m["advisories"]}</a></td>',
            _cell(f"{_run_link(m['newest_run'], m['newest_started'], runs_dir)}"
                  f"<div class=\"case\">{outcome_html(m['newest_exit'])}</div>"),
        ]

    module_headers = ['Module', 'Runs', 'Pass rate', 'Passed / Failed',
                      ('Mean full run', FULL_RUN_TITLE), ('Notes', NOTES_TITLE), 'Newest run']
    # One block per product: its heading, the verdict lines of its
    # modules, then their table.
    groups = []
    for group in agg['groups']:
        verdict = ''.join(f'<li>{escape(module_verdict(m))}</li>' for m in group['modules'])
        groups.append(
            f'<h3 class="product-group">{escape(group["product"])} '
            f'({plural(len(group["modules"]), "module", "modules")})</h3>'
            f'<ul class="verdict">{verdict}</ul>'
            + _table(module_headers, [module_row(m) for m in group['modules']], ''))
    if not agg['modules']:
        groups.append(f'<ul class="verdict"><li>{escape(health_verdict(agg)[0])}</li></ul>'
                      + _table(module_headers, [], 'No archived runs yet. Run the suite once.'))
    tiles = ''.join(_tile(p, runs_dir) for p in agg['products'])
    tiles_html = (f'<div class="tiles">{tiles}</div>' if tiles
                  else '<p class="empty">No archived runs yet. Run the suite once.</p>')
    watch_rows = [[
        _cell(_test_cell(w)),
        _cell(f'{escape(w["module_name"])}<div class="nodeid">{escape(w["module"])}</div>'),
        _cell(render_strip(w['facts'], '', with_text=False, links=RunLinks(runs_dir))),
        _rate_cell(w['facts']), _cell(_failure_link(w['facts']['last_failure'], runs_dir)),
    ] for w in agg['watch']]
    # An empty table must not claim a clean window when the failures sit
    # in runs that carry no per-test records.
    unnamed = window['unrecorded_failing_runs'] if not agg['watch'] else 0
    no_watch = (f'No test named: the failures in this window are in {plural(unnamed, "run", "runs")} '
                'archived without per-test records.' if unnamed
                else 'None: no test failed in the window.')
    intro = ''.join(
        f'<p class="case">{escape(line).replace(REBUILD_COMMAND, f"<code>{REBUILD_COMMAND}</code>")}</p>'
        for line in intro_lines(agg, generated))
    return f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Test Health</title>
{favicon_link()}
<style>
.brand-row {{ display: flex; align-items: center; gap: 1rem; margin-bottom: 0.5rem; }}
.brand-row h1 {{ margin: 0; }}
.brand {{ display: block; }}{PAGE_CSS}
p.case {{ margin: .15rem 0; }}
.tiles {{ display: flex; flex-wrap: wrap; gap: .75rem; margin: .5rem 0 1rem; }}
.tile {{ background: var(--card); border: 1px solid var(--line); border-radius: 6px;
  padding: .75rem 1rem; min-width: 15rem; }}
.tile-name {{ font-weight: 600; font-size: 1.05rem; margin-bottom: .4rem; }}
.tile-fact {{ display: flex; justify-content: space-between; gap: 1.5rem; font-size: .9rem; }}
.tile-fact .k {{ color: var(--muted); }}
.tile-fact .v {{ font-variant-numeric: tabular-nums; }}
h3.product-group {{ font-size: 1.05rem; margin: 1.25rem 0 .25rem; }}
th[title] {{ text-decoration: underline dotted; cursor: help; }}
details.runs {{ margin-top: .2rem; font-size: .85rem; }}
details.runs summary {{ cursor: pointer; color: var(--muted); }}
details.runs ul {{ margin: .25rem 0 0; padding-left: 1.1rem; }}
</style>
</head>
<body>
<div class="brand-row">{header_logo()}<h1>Test Health</h1></div>
{live_record.banner_html()}
{intro}
<div class="meta"><a href="index.html">Archive Index</a></div>

<h2 id="products">Products ({len(agg['products'])})</h2>
{tiles_html}

<h2 id="modules">Modules ({len(agg['modules'])})</h2>
{''.join(groups)}

<h2 id="watch">Tests to watch ({len(agg['watch'])})</h2>
<p class="case">Every test that failed at least once in its last {HISTORY_RUNS}
results. The strip shows those results, oldest left and newest right (hover a block for the run,
click it to open that run). The flaky rate is failures / runs the test was present in. A test is
flaky when that window holds both a pass and a failure.</p>
{_table(['Test', 'Module', 'Last runs', 'Flaky rate', 'Last failure'], watch_rows, no_watch)}
</body>
</html>
'''


def format_table(agg: dict) -> str:
    """The terminal view: the products, the modules under each, the
    module table, then the tests to watch."""
    window = agg['window']
    out = [f"Health: newest {window['last_n']} runs of each module. "
           + (span_line(window) if window['runs'] else 'No runs.')]
    out.extend(product_verdict(p) for p in agg['products'])
    for group in agg['groups']:
        out.append('')
        out.append(f"{group['product']} ({plural(len(group['modules']), 'module', 'modules')})")
        out.extend(f"  {module_verdict(m)}"
                   + (f" Products: {', '.join(m['products'])}." if m['mixed'] else '')
                   for m in group['modules'])
    if not agg['modules']:
        out.append('')
        out.extend(health_verdict(agg))

    def cut(text: str) -> str:
        return text if len(text) <= NAME_WIDTH else text[:NAME_WIDTH - 3] + '...'

    out.append('')
    out.append(f"{'MODULE':<{NAME_WIDTH}} {'RUNS':>4} {'PASS':>5} {'FULL RUN':>9} {'NOTES':>5}  NEWEST")
    for m in agg['modules']:
        out.append(f"{cut(m['name']):<{NAME_WIDTH}} {m['runs']:>4} {_pct(m['pass_rate']):>5} "
                   f"{_secs(m['mean_full_run_s']):>9} {m['advisories']:>5}  "
                   f"{friendly_time(m['newest_started'])} {exit_outcome(m['newest_exit'])}")
    if not agg['modules']:
        out.append('(no archived runs)')
    out.append('')
    out.append(f"Tests to watch ({len(agg['watch'])}):")
    for w in agg['watch']:
        facts = w['facts']
        last = friendly_time(facts['last_failure']['started']) if facts['last_failure'] else '-'
        out.append(f"  {_pct(facts['rate']):>4}  {facts['failed']}/{facts['runs']} failed  "
                   f"last {last}  {w['title']} ({w['module_name']})")
    return '\n'.join(out)


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None, root=None) -> int:
    if root is not None:
        run_archive.configure(root)
    parser = argparse.ArgumentParser(
        prog='health_summary.py', description=__doc__.split('\n\n', 1)[1],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--last', type=int, default=DEFAULT_LAST,
                        help=f'newest runs of each module to read (default {DEFAULT_LAST})')
    parser.add_argument('--runs-dir', type=Path, default=run_archive.RUNS_DIR,
                        help=f'the run archive (default {run_archive.RUNS_DIR})')
    args = parser.parse_args(argv)
    if args.last < 1:
        parser.error('--last must be 1 or more')

    runs = load_runs(args.runs_dir)
    # The mapping file's project_key: the product a run names when neither
    # its records nor its targets do.
    agg = aggregate(runs, args.last, default_key=load_project_key(run_archive.MAPPING_FILE))
    print(format_table(agg))
    if not runs:
        print(f'\nNo archived runs under {args.runs_dir}. Run the suite once.')
        return 0
    page = args.runs_dir / 'health.html'
    page.write_text(render_health(agg, runs_dir=args.runs_dir), encoding='utf-8')
    # The index links health.html once it exists.
    _write_index(args.runs_dir)
    print(f'\nHealth page: {page.resolve()}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
