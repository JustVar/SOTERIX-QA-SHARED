"""The run page in 3 levels: the verdict, the causes and the map, then 1 test in a side pane.

The page before this one showed each fact of each failure at the same time: a summary
sentence, a banner, 12 run facts, 4 count pills, 5 links and, for each failure, 8 to 10
items. A reader looked for the one thing they came for (Justin, 09/21/2026). This page
shows a level only when the reader asks for it:

1. The verdict: 1 sentence, the count of causes and 3 or 4 facts of the run.
2. The causes (failures that share 1 error are 1 line), a map when the run has a
   second axis (the Camera 2 Cloud suite runs each test on each camera), and the
   tests as 1-line rows behind the Failed / Skipped / Passed / All tabs.
3. One test: a click on a cause row, a map square or a test row puts that test in
   the side pane, under the tabs Error, Evidence, History and Identity. The pane
   shows the run's details while no test is selected, so nothing moves the page.

Each test's pane content is rendered here, in Python, into a <template> element. The
browser does not render a template or load its images until the script copies it into
the pane, so a page with 300 screenshots opens as fast as one with none.

run_report.build_run_report collects the facts and calls render_run_page. This module
reads no file except to ask which linked files are there.
"""
from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import TYPE_CHECKING, Any

from soterix_qa_shared.qatouch.mapping import canonical_nodeid
from soterix_qa_shared.reporting.branding import favicon_link, header_logo

if TYPE_CHECKING:
    from soterix_qa_shared.reporting.run_report import RunLinks, TestRecord

# The 2 views of the clock switch: the team's own time on the 12 hour clock, and India
# time on the 24 hour clock (Justin, 09/21/2026). The zone is named in both views: people
# in 2 countries read this page, and a bare "15:55" does not say whose clock it is.
ZONE_12H = 'America/Chicago'
ZONE_24H = ('Asia/Kolkata', 'IST')

BROWSERS = ('chromium', 'firefox', 'webkit', 'chrome', 'msedge')
MAP_MAX_COLUMNS = 40
_FOLDER_STAMP_RE = re.compile(r'^(\d{4}-\d{2}-\d{2})[ _T](\d{2})-(\d{2})-(\d{2})$')
_UTC_OFFSET_RE = re.compile(r'^([+-])(\d{2}):?(\d{2})$')

# The consumer's names for the map's columns. The package knows a parameter only as text
# ('dc-5v-1-q300c5b44'). The WebApp repo knows that it is a camera, its model and its address.
AxisProvider = Callable[[set[str]], Mapping[str, Any] | None]
_axis_provider: AxisProvider | None = None


def configure_axis(provider: AxisProvider | None) -> None:
    """Register the function that names the columns: it gets the run's parameters and returns
    {'noun': 'camera', 'columns': {param: {'group': 'DC-5V-1', 'label': '.129', 'title': '...'}}},
    or None for a run whose parameters it does not know."""
    global _axis_provider
    _axis_provider = provider


def _provided_axis(params: set[str]) -> Mapping[str, Any]:
    if _axis_provider is None:
        return {}
    try:
        given = _axis_provider(set(params))
    # The names are a nicety of 1 section. A registry file that is gone or does not parse must
    # not cost the run its page, so the map falls back to the parameter text.
    except Exception:  # noqa: BLE001
        return {}
    return given if isinstance(given, Mapping) else {}


# ---------------------------------------------------------------- time

def utc_iso(naive: str | None, utc_offset: str | None = None) -> str:
    """The run's start as UTC ('2026-09-21T10:25:54Z'), or '' when the text is no time.

    summary.json holds the time of the machine that ran the tests, with no zone. A run
    since schema 6 records its `utc_offset`. For an older run the offset is the one of
    the machine that builds the page, on that date: the box builds the pages of its own
    runs, and both are on UTC.
    """
    text = str(naive or '').strip()
    stamp = _FOLDER_STAMP_RE.match(text)
    if stamp:
        text = '{}T{}:{}:{}'.format(*stamp.groups())
    try:
        local = datetime.fromisoformat(text.replace(' ', 'T'))
    except ValueError:
        return ''
    if local.tzinfo is None:
        m = _UTC_OFFSET_RE.match(str(utc_offset or ''))
        if m:
            delta = timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
            local = local.replace(tzinfo=timezone(delta if m.group(1) == '+' else -delta))
        else:
            local = local.astimezone()
    return local.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')


def time_html(naive: str | None, utc_offset: str | None, fallback: str, short: bool = False) -> str:
    """A <time> the script rewrites in the reader's chosen view; `fallback` shows with no script.
    `short` leaves out the year and the zone name: the heading of the page names the zone."""
    stamp = utc_iso(naive, utc_offset)
    attr = f' data-utc="{escape(stamp, quote=True)}"' if stamp else ''
    attr += ' data-short' if stamp and short else ''
    return f'<time{attr}>{escape(fallback)}</time>'


# ---------------------------------------------------------------- the second axis

def param_of(nodeid: str) -> str:
    """The test's parameter without the browser: 'dc-5v-1-q300c5b44' of
    'test_x[dc-5v-1-q300c5b44-chromium]', and '' of 'test_x[chromium]'."""
    nodeid = canonical_nodeid(nodeid)
    if not nodeid.endswith(']') or '[' not in nodeid:
        return ''
    parts = [p for p in nodeid[nodeid.rindex('[') + 1:-1].split('-')]
    while parts and parts[-1].lower() in BROWSERS:
        parts.pop()
    while parts and parts[0].lower() in BROWSERS:
        parts.pop(0)
    return '-'.join(parts)


def function_of(nodeid: str) -> str:
    nodeid = canonical_nodeid(nodeid)
    return nodeid[:nodeid.rindex('[')] if nodeid.endswith(']') and '[' in nodeid else nodeid


def _fallback_column(param: str) -> dict:
    """With no names from the consumer: the group is the parameter without its last part,
    and the label is the end of that last part ('dc-5v-1' and '5b44')."""
    head, _, tail = param.rpartition('-')
    return {'group': head.upper(), 'label': tail[-4:] if len(tail) > 4 else tail, 'title': param}


def find_axis(records: Sequence[TestRecord], summary: Mapping[str, Any]) -> dict | None:
    """The run's second axis, or None when it has none.

    {'noun', 'columns': [{'param', 'group', 'label', 'title'}], 'rows': [(module, title, {param: record})]}.
    A run has an axis when its tests have 2 or more parameters and at least half of all its
    test functions ran with 2 or more of them. The names come from summary['axis'] when the
    run recorded them, else from the consumer's provider (configure_axis), else from the
    parameter text.
    """
    rows: dict[str, dict[str, TestRecord]] = {}
    for rec in records:
        param = param_of(rec.nodeid)
        if param:
            rows.setdefault(function_of(rec.nodeid), {})[param] = rec
    params = {p for cells in rows.values() for p in cells}
    if len(params) < 2 or len(params) > MAP_MAX_COLUMNS or len(rows) < 2:
        return None
    # Of ALL the run's test functions, not only the ones with a parameter: a tests/ui run has a
    # few parametrized tests (search categories), and they are not an axis of the run.
    functions = {function_of(rec.nodeid) for rec in records}
    if sum(1 for cells in rows.values() if len(cells) >= 2) * 2 < len(functions):
        return None
    given = summary.get('axis') if isinstance(summary.get('axis'), dict) else _provided_axis(params)
    named = given.get('columns') if isinstance(given.get('columns'), dict) else {}
    columns = []
    for param in params:
        meta = dict(_fallback_column(param))
        if isinstance(named.get(param), dict):
            meta.update({k: str(v) for k, v in named[param].items() if v})
        columns.append({'param': param, **meta})
    columns.sort(key=lambda c: (c['group'], [int(n) for n in re.findall(r'\d+', c['label'])] or [0], c['label']))
    ordered = sorted(rows.items(), key=lambda kv: (next(iter(kv[1].values())).module_name, kv[0]))
    return {'noun': str(given.get('noun') or 'variant'), 'columns': columns,
            'rows': [(next(iter(cells.values())).module_name, next(iter(cells.values())).display_title, cells)
                     for _fn, cells in ordered]}


def column_name(axis: dict | None, nodeid: str) -> str:
    """How the pane and the rows name a test's column: 'BC-5V-1 .171', or the parameter."""
    param = param_of(nodeid)
    if not axis or not param:
        return param
    for col in axis['columns']:
        if col['param'] == param:
            return f"{col['group']} {col['label']}".strip()
    return param


# ---------------------------------------------------------------- causes

def shared_line(lines: Sequence[str]) -> str:
    """One line for several errors that differ in a name or a number: the words they
    share, with '...' where they differ. 'Camera 10.2.0.129 reports' and 'Camera
    10.2.0.171 reports' read as 'Camera ... reports'."""
    lines = [line for line in lines if line]
    if not lines:
        return ''
    tokens = [line.split() for line in lines]
    if len({len(t) for t in tokens}) != 1:
        return lines[0]
    out: list[str] = []
    for column in zip(*tokens):
        word = column[0] if len(set(column)) == 1 else '...'
        if not (word == '...' and out and out[-1] == '...'):
            out.append(word)
    return ' '.join(out)


# ---------------------------------------------------------------- page parts

def bucket(outcome: str) -> str:
    """The tab a test is under: fail, skip, pass or unkn."""
    from soterix_qa_shared.reporting.run_report import (
        FAILURE_OUTCOMES,
        PASSED_OUTCOMES,
        SKIPPED_OUTCOMES,
    )
    if outcome in FAILURE_OUTCOMES:
        return 'fail'
    if outcome in SKIPPED_OUTCOMES:
        return 'skip'
    return 'pass' if outcome in PASSED_OUTCOMES else 'unkn'


def _kv(rows: Sequence[tuple[str, str]]) -> str:
    """Label and value rows of the pane. A value is HTML; a row with no value is left out."""
    cells = ''.join(f'<span>{escape(label)}</span><span>{value}</span>' for label, value in rows if value)
    return f'<div class="kv">{cells}</div>' if cells else ''


def _lab(text: str) -> str:
    return f'<span class="lab">{escape(text)}</span>'


def _test_template(index: int, rec: TestRecord, run_dir: Path, links: RunLinks,
                   axis: dict | None, with_product: bool, has_run_log: bool = False) -> str:
    """The pane content of 1 test, in a <template> the browser leaves alone until it is asked for."""
    from soterix_qa_shared.reporting.run_report import (
        FAILURE_OUTCOMES,
        LAST_ACTIONS,
        NOTES_TITLE,
        _href,
        _render_actions,
        _render_shots,
        failure_digest,
        failure_headline,
        friendly_time,
        outcome_label,
        render_strip,
    )
    failed = rec.outcome in FAILURE_OUTCOMES
    where = column_name(axis, rec.nodeid)
    top = ' &middot; '.join(escape(part) for part in (outcome_label(rec.outcome), where, rec.duration_text) if part)
    head = (f'<span class="lab tone-{bucket(rec.outcome)}">{top}</span><h3>{escape(rec.display_title)}</h3>'
            f'<span class="sub">{escape(rec.module_name)}</span>')

    # --- Error (Result for a test that did not fail)
    first = 'Error' if failed else 'Result'
    result = []
    if failed:
        digest = rec.digest if rec.digest is not None else failure_digest(rec, run_dir)
        headline = rec.headline or failure_headline(rec, digest)
        result.append(f'<pre class="headline">{escape(headline)}</pre>' if headline
                      else '<p class="sub">No error message recorded.</p>')
        if rec.log:
            result.append('<details class="more"><summary>Full traceback and captured output</summary>'
                          f'<pre>{escape(rec.log)}</pre></details>')
    else:
        digest = None
        if rec.reason:
            result.append(_lab('Skipped because') + f'<pre>{escape(rec.reason)}</pre>')
        else:
            result.append(f'<p class="sub">{escape(outcome_label(rec.outcome))}. No error.</p>')
        if rec.reruns:
            result.append(f'<p class="sub">Passed on a rerun ({rec.reruns}).</p>')
    if rec.advisories:
        result.append(f'<span class="lab" title="{escape(NOTES_TITLE, quote=True)}">Notes</span><ul class="plain">'
                      + ''.join(f'<li>{escape(a)}</li>' for a in rec.advisories) + '</ul>')

    # --- Evidence
    evidence = []
    shots = _render_shots(rec)
    if shots:
        evidence.append(_lab('Screenshots') + shots)
    if digest is not None:
        evidence.append(_lab(f'Playwright actions (last {LAST_ACTIONS}, failing one last)'))
        if digest.get('url'):
            evidence.append(f'<p class="sub">Final URL: <code>{escape(str(digest["url"]))}</code></p>')
        evidence.append(_render_actions(digest) or '<p class="sub">No actions in the trace.</p>')
        console = digest.get('console') or []
        if console:
            evidence.append(_lab(f'Console errors ({len(console)})') + '<ul class="plain">' + ''.join(
                f'<li><code>{escape(c)}</code></li>' for c in console[:20]) + '</ul>')
    for rel in rec.videos:
        evidence.append(_lab('Video') + f'<video controls preload="none" src="{_href(rel)}"></video>')
    if failed and not rec.videos:
        evidence.append(_lab('Video') + '<p class="sub">No video kept for this test.</p>')
    if failed and not rec.traces:
        evidence.append(_lab('Trace') + '<p class="sub">No trace kept for this test.</p>')
    for rel in rec.traces:
        label = 'Trace zip' if rel.lower().endswith('.zip') else Path(rel).name
        evidence.append(_lab('Trace') + f'<div><a href="{_href(rel)}">{escape(label)}</a></div>')
        if rel.lower().endswith('.zip'):
            evidence.append(f'<pre>python -m playwright show-trace "{escape(os.path.abspath(run_dir / rel))}"</pre>')
    if rec.log_file:
        evidence.append(_lab('Captured output') + f'<div><a href="{_href(rec.log_file)}">{escape(Path(rec.log_file).name)}</a></div>')
    if not failed and has_run_log:
        evidence.append(_lab('Captured output') + '<p class="sub">The captured output of passed and skipped tests is in '
                        '<a href="log/run.log">log/run.log</a>, one section per test.</p>')
    if not evidence:
        evidence.append('<p class="sub">This run kept no screenshot, trace or log for this test.</p>')

    # --- History
    history = []
    strip = render_strip(rec.history, '../', links=links)
    history.append(strip or '<p class="sub">No earlier run of this test is in the archive.</p>')
    facts = []
    if rec.delta:
        facts.append(('Since the previous run', escape(rec.delta)))
    if failed and rec.signature:
        if rec.seen is None:
            facts.append(('This error', 'New: no earlier run in the archive shows it'))
        else:
            last = time_html(rec.seen.get('started'), rec.seen.get('utc_offset'), friendly_time(rec.seen.get('started')), short=True)
            facts.append(('This error', f'Seen before, last on <a href="{links.href(rec.seen["run"], "../")}">{last}</a>'))
    history.append(_kv(facts))

    # --- Identity
    case = f'{rec.case_key} {rec.case_title}'.strip()
    phases = ', '.join(f'{name} {seconds} s' for name, seconds in (rec.phases or {}).items())
    identity = _kv([('Node id', f'<code>{escape(rec.nodeid)}</code>'), ('QA Touch', escape(case)),
                    ('Module', escape(rec.module_name)), ('Product', escape(rec.product) if with_product else ''),
                    ('Duration', escape(rec.duration_text)), ('Phases', escape(phases))])

    tabs = ((first, result), ('Evidence', evidence), ('History', history), ('Identity', [identity]))
    bar = ''.join(f'<button type="button" data-tab="{i}">{escape(name)}</button>' for i, (name, _b) in enumerate(tabs))
    body = ''.join(f'<section data-tab-body="{i}">{"".join(parts)}</section>' for i, (_n, parts) in enumerate(tabs))
    return f'<template id="t{index}">{head}<div class="tabs">{bar}</div>{body}</template>'


def _run_template(summary: Mapping[str, Any], run_dir: Path, product: str, target: str, duration: str, started: str,
                  file_links: Sequence[tuple[str, str]], compared: str, unattached: Sequence[str]) -> str:
    """The pane's resting content: the facts of the run in 4 groups."""
    from soterix_qa_shared.reporting.run_report import (
        _href,
        exit_outcome,
        friendly_stamp,
    )
    prov = summary.get('provenance') if isinstance(summary.get('provenance'), dict) else {}
    offset = summary.get('utc_offset')

    def text(key: str) -> str:
        return escape(str(prov.get(key) or ''))

    browser = ' / '.join(str(v).capitalize() for v in (prov.get('browser'), prov.get('browser_channel')) if v)
    version = text('browser_version')
    latest = text('browser_latest')
    if version and latest and latest != version:
        version += f' <span class="sub">(stable {latest} is out)</span>'
    groups = (
        ('Run', [('Outcome', escape(exit_outcome(summary.get('exitstatus')))), ('Product', escape(product)),
                 ('Target', escape(target)),
                 ('Started', time_html(started, offset, friendly_stamp(started))),
                 ('Ended', time_html(summary.get('ended'), offset, friendly_stamp(summary.get('ended')))
                  if summary.get('ended') else ''),
                 ('Duration', escape(duration)), ('Commit', text('git_commit')), ('Folder', escape(run_dir.name))]),
        ('Where', [('Environment', text('env').upper()), ('Box', text('box')), ('Tenant', text('base_url_host')),
                   ('Machine', text('host')), ('Operator', text('operator')), ('Account', text('account'))]),
        ('Browser', [('Browser', escape(browser)), ('Version', version), ('Playwright', text('playwright')),
                     ('pytest-playwright', text('pytest_playwright'))]),
        ('Files', [(label, html) for label, html in file_links]),
    )
    parts = [_lab('This run'), '<h3>Run details</h3>']
    for name, rows in groups:
        block = _kv(rows)
        if block:
            parts.append(_lab(name) + block)
    if compared:
        parts.append(_lab('Compared with') + f'<p class="sub">{compared}</p>')
    if unattached:
        parts.append(f'<details class="more"><summary>Files not matched to a test ({len(unattached)})</summary>'
                     '<ul class="plain">' + ''.join(f'<li><a href="{_href(u)}">{escape(u)}</a></li>' for u in unattached)
                     + '</ul></details>')
    return '<template id="t-run">' + ''.join(parts) + '</template>'


def _notes_template(advisories: Sequence[Mapping[str, Any]]) -> str:
    from soterix_qa_shared.reporting.run_report import NOTES_TITLE
    body = ('<ul class="plain">' + ''.join(
        f"<li><code>{escape(str(a.get('test', '')))}</code><br>{escape(str(a.get('line', '')))}</li>"
        for a in advisories) + '</ul>') if advisories else '<p class="sub">None.</p>'
    return (f'<template id="t-notes">{_lab("This run")}<h3>Notes ({len(advisories)})</h3>'
            f'<p class="sub">{escape(NOTES_TITLE)}</p>{body}</template>')


def _slowest_template(records: Sequence[TestRecord], ids: Mapping[str, int], axis: dict | None, count: int) -> str:
    from soterix_qa_shared.reporting.run_report import format_seconds
    timed = sorted((r for r in records if r.seconds is not None), key=lambda r: r.seconds or 0.0, reverse=True)[:count]
    rows = ''.join(
        f'<div class="row" role="button" tabindex="0" data-id="{ids[r.nodeid]}"><span class="g {bucket(r.outcome)}"></span>'
        f'<span class="t">{escape(r.display_title)}</span><span class="m">{escape(column_name(axis, r.nodeid))}</span>'
        f'<span class="m">{escape(format_seconds(r.seconds or 0.0))}</span></div>' for r in timed)
    return (f'<template id="t-slowest">{_lab("This run")}<h3>Slowest {count}</h3>'
            + (rows or '<p class="sub">No per-test timing in this run.</p>') + '</template>')


def _verdict(failed: int, passed: int, skipped: int, causes: int, records: Sequence[TestRecord],
             summary: Mapping[str, Any]) -> tuple[str, str]:
    """The large line and the line under it."""
    from soterix_qa_shared.reporting.run_report import (
        DELTA_FIXED,
        DELTA_NEW_FAILURE,
        DELTA_STILL_FAILING,
        PASSED_OUTCOMES,
        plural,
    )
    total = failed + passed + skipped
    if total == 0:
        return '<em class="tone-skip">No tests ran.</em>', ''
    if failed:
        big = (f'<em class="tone-fail">{failed} of {total} failed.</em> '
               f'{plural(causes, "cause", "causes")}.')
    elif skipped:
        big = f'<em class="tone-pass">{passed} of {total} passed.</em>'
    else:
        big = f'<em class="tone-pass">All {total} passed.</em>'
    labels = [r.delta for r in records]
    parts = []
    counts = [p for p in (f'{passed} passed' if failed and passed else '', f'{skipped} skipped' if skipped else '') if p]
    if counts:
        parts.append(', '.join(counts) + '.')
    changes = [(labels.count(DELTA_NEW_FAILURE), 'new failure', 'new failures'),
               (labels.count(DELTA_STILL_FAILING), 'still failing', 'still failing'),
               (labels.count(DELTA_FIXED), 'fixed', 'fixed')]
    detail = ', '.join(plural(n, one, many) for n, one, many in changes if n)
    if detail:
        parts.append(detail[0].upper() + detail[1:] + ' since the previous run.')
    reruns = sum(1 for r in records if r.reruns and r.outcome in PASSED_OUTCOMES)
    if reruns:
        parts.append(plural(reruns, 'test passed on a rerun', 'tests passed on a rerun') + '.')
    skip = summary.get('one_skip_reason') if isinstance(summary.get('one_skip_reason'), dict) else None
    if skip:
        parts.append(f"{skip.get('count')} skipped for one reason: {skip.get('reason')}")
    sub = escape(' '.join(parts))
    try:
        not_run = int(summary.get('not_run_count') or 0)
    # a count that is not a number is read as no count: the page still renders
    except (TypeError, ValueError):
        not_run = 0
    if not_run:
        sub += (f' <b class="tone-fail">{not_run} of {escape(str(summary.get("collected")))} collected tests did '
                f'not run. The counts cover only what ran.</b>')
    return big, sub


def render_run_page(run_dir: Path, summary: dict, tests: Mapping[str, TestRecord], unattached: list[str],
                    previous: Mapping | None = None, product: str = '') -> str:
    from soterix_qa_shared.reporting.products import is_mixed
    from soterix_qa_shared.reporting.run_report import (
        FAILURE_OUTCOMES,
        SLOWEST_COUNT,
        RunLinks,
        delta_wording,
        duration_text,
        first_line,
        friendly_stamp,
        friendly_time,
        group_failures,
        plural,
        run_href,
    )
    run_dir = Path(run_dir)
    runs_dir = run_dir.parent
    links = RunLinks(runs_dir, run_dir.name)
    with_product = is_mixed(product)
    records = sorted(tests.values(), key=lambda t: (t.module_name, t.nodeid))
    ids = {rec.nodeid: i for i, rec in enumerate(records)}
    axis = find_axis(records, summary)
    started = summary.get('started') or run_dir.name.partition('__')[0].replace('_', ' ')
    target = ' '.join(str(t) for t in (summary.get('target') or [])) or run_dir.name.partition('__')[2]
    duration = duration_text(summary.get('duration_s'))
    prov = summary.get('provenance') if isinstance(summary.get('provenance'), dict) else {}
    when = friendly_stamp(started)
    title = f'{product} - Test Run - {when}' if product else f'Test Run - {when}'

    counts = {'fail': 0, 'skip': 0, 'pass': 0, 'unkn': 0}
    for rec in records:
        counts[bucket(rec.outcome)] += 1
    failures = [r for r in records if r.outcome in FAILURE_OUTCOMES]
    groups = sorted(group_failures(failures), key=len, reverse=True)
    big, sub = _verdict(counts['fail'], counts['pass'], counts['skip'], len(groups), records, summary)

    # --- causes
    noun = axis['noun'] if axis else ''
    cause_html = []
    for group in groups:
        line = shared_line([first_line(r.headline or r.error) for r in group]) or 'No error message recorded.'
        columns = sorted({column_name(axis, r.nodeid) for r in group if param_of(r.nodeid)})
        where = plural(len(columns), noun, noun + 's') if axis and columns else plural(len(group), 'test', 'tests')
        lead = group[0]
        if not lead.signature:
            seen = ''
        elif lead.seen is None:
            seen = '<span class="new">new</span>'
        else:
            seen = 'since ' + time_html(lead.seen.get('started'), lead.seen.get('utc_offset'),
                                        friendly_time(lead.seen.get('started')), short=True)
        items = ''.join(
            f'<li role="button" tabindex="0" data-id="{ids[r.nodeid]}"><span class="g fail"></span>{escape(r.display_title)} '
            f'<i>{escape(column_name(axis, r.nodeid))}</i></li>' for r in group)
        cause_html.append(
            f'<details class="cause"><summary><b>{len(group)}</b><span class="msg">{escape(line)}</span>'
            f'<span class="where">{escape(where)}<br>{seen}</span></summary>'
            + (f'<p class="cols">{escape(", ".join(columns))}</p>' if columns else '') + f'<ul>{items}</ul></details>')
    causes_block = f'<div id="causes"><h2>Causes</h2>{"".join(cause_html)}</div>' if cause_html else ''

    # --- map
    map_block = ''
    if axis:
        spans: dict[str, int] = {}
        for col in axis['columns']:
            spans[col['group']] = spans.get(col['group'], 0) + 1
        edges = set()
        seen_groups: set[str] = set()
        for i, col in enumerate(axis['columns']):
            if col['group'] not in seen_groups:
                seen_groups.add(col['group'])
                edges.add(i)
        head_groups = ''.join(f'<th class="model" colspan="{n}">{escape(g)}</th>' for g, n in spans.items())
        head_cols = ''.join(
            f'<th class="cam{" edge" if i in edges else ""}" data-col="{i}" title="{escape(col["title"], quote=True)}">'
            f'{escape(col["label"])}</th>' for i, col in enumerate(axis['columns']))
        body, last_module = [], None
        for module, row_title, cells in axis['rows']:
            if module != last_module:
                body.append(f'<tr class="mod"><td colspan="{len(axis["columns"]) + 1}">{escape(module)}</td></tr>')
                last_module = module
            tds = []
            for i, col in enumerate(axis['columns']):
                rec = cells.get(col['param'])
                edge = ' class="edge"' if i in edges else ''
                if rec is None:
                    tds.append(f'<td{edge} data-col="{i}"></td>')
                    continue
                tds.append(f'<td{edge} data-col="{i}"><button type="button" class="g {bucket(rec.outcome)}" '
                           f'data-id="{ids[rec.nodeid]}" aria-label="{escape(rec.outcome)}: {escape(col["title"], quote=True)}">'
                           '</button></td>')
            body.append(f'<tr><th>{escape(row_title)}</th>{"".join(tds)}</tr>')
        nouns = noun + 's'
        map_block = (
            f'<div id="map"><h2>Map: {len(axis["rows"])} tests on {len(axis["columns"])} {escape(nouns)}</h2>'
            '<p id="mapline"><span id="mapcount"></span> <button type="button" id="maptoggle"></button></p>'
            f'<p class="sub key">A column is 1 {escape(noun)}, under its group. Point at a column name for its full name.</p>'
            f'<div class="scroll"><table><thead><tr><th></th>{head_groups}</tr><tr><th></th>{head_cols}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div></div>')

    # --- rows and pane templates
    has_run_log = (run_dir / 'log' / 'run.log').is_file()
    rows, templates, last_module = [], [], None
    for rec in records:
        if rec.module_name != last_module:
            rows.append(f'<div class="modrow">{escape(rec.module_name)}</div>')
            last_module = rec.module_name
        key = ' '.join(w for w in (rec.display_title, rec.nodeid, rec.case_key, rec.case_title, rec.module_name,
                                   rec.outcome, rec.delta, rec.product, column_name(axis, rec.nodeid)) if w).lower()
        right = ' · '.join(part for part in (rec.product if with_product else '', column_name(axis, rec.nodeid)) if part)
        rows.append(
            f'<div class="row" role="button" tabindex="0" id="{escape(rec.safe, quote=True)}" data-id="{ids[rec.nodeid]}" '
            f'data-o="{bucket(rec.outcome)}" data-test="{escape(key, quote=True)}"><span class="g {bucket(rec.outcome)}"></span>'
            f'<span class="t">{escape(rec.display_title)}</span><span class="m">{escape(rec.delta)}</span>'
            f'<span class="m">{escape(right)}</span><span class="m">{escape(rec.duration_text)}</span></div>')
        templates.append(_test_template(ids[rec.nodeid], rec, run_dir, links, axis, with_product, has_run_log))

    # --- run details, notes, slowest
    file_links = []
    if (run_dir / 'report.html').is_file():
        file_links.append(('Report', '<a href="report.html">Raw pytest-html</a>'))
    if has_run_log:
        file_links.append(('Log', '<a href="log/run.log">run.log</a>'))
    if (run_dir / 'advisories.txt').is_file():
        file_links.append(('Notes', '<a href="advisories.txt">advisories.txt</a>'))
    file_links.append(('Archive', '<a href="../index.html">Archive index</a>'))
    if (runs_dir / 'health.html').is_file():
        file_links.append(('Health', '<a href="../health.html">Health page</a>'))
    if previous:
        file_links.append(('Before', f'<a href="{run_href(previous["name"], runs_dir, "../")}">Previous run of these tests</a>'))
    file_links.append(('Folder', '<a href="./">Files of this run</a>'))
    if previous:
        compared = (f'Changes since the <a href="{run_href(previous["name"], runs_dir, "../")}">previous run of these '
                    f'tests</a>, {time_html(previous.get("started"), previous.get("utc_offset"), friendly_time(previous.get("started")), short=True)}.')
    else:
        compared = escape(delta_wording(None))
    advisories = [a for a in (summary.get('advisories') or []) if isinstance(a, dict)]
    templates.append(_run_template(summary, run_dir, product, target, duration, str(started), file_links, compared, unattached))
    templates.append(_notes_template(advisories))
    templates.append(_slowest_template(records, ids, axis, SLOWEST_COUNT))

    exitstatus = summary.get('exitstatus')
    from soterix_qa_shared.reporting.run_report import exit_outcome
    facts = [('Duration', escape(duration)), ('Machine', escape(str(prov.get('host') or ''))),
             ('Box', escape(str(prov.get('box') or ''))), ('Commit', escape(str(prov.get('git_commit') or '')[:7]))]
    if exitstatus not in (0, 1, None):   # Interrupted, Error, Usage: the counts alone do not say it
        facts.insert(0, ('Outcome', f'<b class="tone-fail">{escape(exit_outcome(exitstatus))}</b>'))
    facts_html = ''.join(f'<div><span>{escape(label)}</span>{value}</div>' for label, value in facts if value)

    tabs = [('fail', 'Failed'), ('skip', 'Skipped'), ('pass', 'Passed')]
    if counts['unkn']:
        tabs.append(('unkn', 'Unknown'))
    first_tab = 'fail' if counts['fail'] else 'all'
    nav = ''.join(f'<button type="button" data-f="{key}"{" class=on" if key == first_tab else ""}>{label}<b>{counts[key]}</b></button>'
                  for key, label in tabs)
    nav += f'<button type="button" data-f="all"{" class=on" if first_tab == "all" else ""}>All<b>{len(records)}</b></button>'

    heading = escape(product) + ' &middot; ' if product else ''
    return f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
{favicon_link()}
<style>{RUN_PAGE_CSS}</style>
</head>
<body data-first="{first_tab}" data-zone12="{ZONE_12H}" data-zone24="{ZONE_24H[0]}" data-zone24-name="{ZONE_24H[1]}">
<div class="wrap"><main>
<div class="brand-row">{header_logo(28)}<h1>{heading}{time_html(started, summary.get('utc_offset'), when)}
 <button type="button" id="clock" title="12 hour clock in Central time, or 24 hour clock in India time"></button></h1></div>
<p class="verdict">{big}</p>
<p class="sub">{sub}</p>
<div class="facts">{facts_html}<span class="panelinks"><button type="button" data-pane="t-run">Run details</button>
<button type="button" data-pane="t-notes">Notes ({len(advisories)})</button>
<button type="button" data-pane="t-slowest">Slowest {SLOWEST_COUNT}</button></span></div>
<nav>{nav}<input id="filter" type="search" placeholder="Filter: title, node id, case key, module"
 aria-label="Filter tests"></nav>
{causes_block}
{map_block}
<div><h2 id="list-h">Tests</h2><div id="list">{"".join(rows)}</div></div>
</main><aside id="pane" aria-live="polite"></aside></div>
{"".join(templates)}
<script>{RUN_PAGE_JS}</script>
</body>
</html>
'''


RUN_PAGE_CSS = '''
:root { --ink:#1b1d21; --dim:#6b7078; --rule:#d9dbe0; --bg:#fbfbfa; --fail:#b3261e; --skip:#9a7b1c; --pass:#2f7d4f; --sel:#efeee9; }
@media (prefers-color-scheme: dark) { :root { --ink:#e8e8e6; --dim:#9a9ea6; --rule:#33363c; --bg:#16171a; --fail:#ff8a80; --skip:#d9b64a; --pass:#6fcf97; --sel:#22242a; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.45 "Segoe UI",system-ui,sans-serif; }
a { color:inherit; }
.wrap { display:grid; grid-template-columns:minmax(0,1fr) 460px; min-height:100vh; }
main { padding:24px 32px 80px; min-width:0; }
aside { border-left:1px solid var(--rule); padding:28px 24px 60px; position:sticky; top:0; height:100vh; overflow:auto; }
.brand-row { display:flex; align-items:center; gap:12px; margin:0 0 8px; }
.brand { display:block; }
h1 { font-size:13px; letter-spacing:.08em; text-transform:uppercase; color:var(--dim); margin:0; font-weight:600; }
.verdict { font-size:30px; line-height:1.2; font-weight:600; margin:0; }
.verdict em { font-style:normal; }
.tone-fail { color:var(--fail); } .tone-pass { color:var(--pass); } .tone-skip { color:var(--skip); }
.sub { color:var(--dim); margin:6px 0 0; }
.facts { display:flex; gap:28px; margin:18px 0 0; padding:10px 0; border-top:1px solid var(--ink); border-bottom:1px solid var(--rule); align-items:baseline; flex-wrap:wrap; }
.facts div span, .lab { font-size:11px; letter-spacing:.07em; text-transform:uppercase; color:var(--dim); display:block; }
.panelinks { margin-left:auto; display:flex; gap:18px; }
.panelinks button { all:unset; cursor:pointer; color:var(--dim); font-size:13px; border-bottom:1px dotted var(--dim); }
.panelinks button:hover, .panelinks button.on { color:var(--ink); border-bottom-color:var(--ink); }
#clock { all:unset; cursor:pointer; margin-left:8px; font-size:11px; letter-spacing:.06em; border:1px solid var(--dim); padding:1px 6px; color:var(--dim); }
#clock:hover { color:var(--ink); border-color:var(--ink); } #clock:empty { display:none; }
nav { display:flex; gap:22px; margin:26px 0 0; border-bottom:1px solid var(--rule); align-items:center; flex-wrap:wrap; }
nav button { all:unset; cursor:pointer; padding:8px 0; color:var(--dim); border-bottom:2px solid transparent; }
nav button b { color:var(--ink); font-weight:600; margin-left:4px; }
nav button.on { color:var(--ink); border-bottom-color:var(--ink); }
nav input { margin-left:auto; border:0; border-bottom:1px solid var(--rule); background:transparent; color:var(--ink); padding:6px 2px; width:280px; font:inherit; outline:none; }
h2 { font-size:13px; letter-spacing:.08em; text-transform:uppercase; color:var(--dim); margin:30px 0 6px; font-weight:600; }
.cause { border-bottom:1px solid var(--rule); }
.cause summary { display:grid; grid-template-columns:44px minmax(0,1fr) 170px; gap:8px; padding:11px 0; cursor:pointer; list-style:none; align-items:baseline; }
.cause summary::-webkit-details-marker { display:none; }
.cause summary b { font-size:20px; color:var(--fail); font-variant-numeric:tabular-nums; }
.cause .msg { white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.cause[open] .msg { white-space:normal; overflow-wrap:anywhere; }
.cause .where, .cols, li i { color:var(--dim); font-size:13px; font-style:normal; }
.cause .where { text-align:right; white-space:nowrap; } .cause .new { color:var(--fail); }
.cols { margin:0 0 6px 52px; } .cause ul { margin:0 0 12px 52px; padding:0; list-style:none; }
.cause li, .row { padding:5px 6px; cursor:pointer; } .cause li:hover, .row:hover, .sel { background:var(--sel); }
.g { display:inline-block; width:11px; height:11px; margin-right:8px; vertical-align:-1px; border:0; padding:0; background:transparent; }
.g.fail { background:var(--fail); } .g.pass { background:var(--pass); opacity:.3; border-radius:50%; transform:scale(.55); }
.g.skip { box-shadow:inset 0 0 0 1.5px var(--skip); } .g.unkn { box-shadow:inset 0 0 0 1.5px var(--dim); border-radius:50%; }
#mapline { margin:0 0 12px; font-size:14px; }
#maptoggle { all:unset; cursor:pointer; font-size:12.5px; font-weight:600; letter-spacing:.04em; text-transform:uppercase; border:1.5px solid var(--ink); padding:3px 10px; margin-left:10px; }
#maptoggle:hover { background:var(--ink); color:var(--bg); } #maptoggle:empty { display:none; }
.key { font-size:13px; margin:0 0 14px; } .scroll { overflow-x:auto; }
table { border-collapse:collapse; margin-top:4px; }
thead th { padding:0; } th.model { font-size:12px; font-weight:600; text-align:left; padding:0 0 4px 6px; border-left:1px solid var(--rule); white-space:nowrap; }
th.cam { font:11.5px/1 ui-monospace,Consolas,monospace; color:var(--dim); font-weight:400; min-width:52px; padding:0 0 8px; text-align:center; cursor:default; }
.edge { border-left:1px solid var(--rule); } .hot { background:var(--sel); } th.cam.hot { color:var(--ink); }
tbody th { text-align:left; font-weight:400; padding:2px 18px 2px 0; max-width:520px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; font-size:13.5px; }
tbody td { padding:0; text-align:center; min-width:52px; height:22px; } td .g { margin:0; cursor:pointer; }
td .g:hover, td .g.sel, td .g:focus-visible { outline:2px solid var(--ink); outline-offset:2px; background-clip:padding-box; }
tr.mod td, .modrow { font:11px/1 ui-monospace,Consolas,monospace; color:var(--dim); padding:14px 0 4px; text-align:left; }
tr.off, .off { display:none !important; }
.row { display:grid; grid-template-columns:20px minmax(0,1fr) auto auto 64px; gap:12px; border-bottom:1px solid var(--rule); font-size:14px; align-items:baseline; }
.row .t { white-space:nowrap; overflow:hidden; text-overflow:ellipsis; } .row .m { color:var(--dim); font-size:12.5px; text-align:right; white-space:nowrap; }
aside h3 { margin:0 0 4px; font-size:19px; line-height:1.3; } aside .lab { margin-top:18px; } aside .lab:first-child { margin-top:0; }
aside pre { white-space:pre-wrap; overflow-wrap:anywhere; font:12.5px/1.5 ui-monospace,Consolas,monospace; margin:6px 0 0; }
aside pre.headline { color:var(--fail); }
.tabs { display:flex; gap:18px; border-bottom:1px solid var(--rule); margin-top:18px; }
.tabs button { all:unset; cursor:pointer; padding:6px 0; color:var(--dim); border-bottom:2px solid transparent; font-size:13.5px; }
.tabs button.on { color:var(--ink); border-bottom-color:var(--ink); }
aside section { display:none; padding-top:12px; } aside section.on { display:block; }
.kv { display:grid; grid-template-columns:118px minmax(0,1fr); gap:5px 12px; margin:6px 0 0; font-size:13.5px; }
.kv > span:nth-child(odd) { color:var(--dim); } .kv span { overflow-wrap:anywhere; }
code { font:12px ui-monospace,Consolas,monospace; overflow-wrap:anywhere; }
details.more { margin-top:14px; } details.more summary { cursor:pointer; color:var(--dim); font-size:13px; }
ul.plain { margin:6px 0 0; padding-left:18px; font-size:13.5px; }
.shots { display:grid; grid-template-columns:repeat(auto-fill,minmax(180px,1fr)); gap:10px; margin-top:8px; }
.shots figure { margin:0; } .shots img { width:100%; display:block; border:1px solid var(--rule); } .shots figcaption { font-size:12px; color:var(--dim); }
video { width:100%; margin-top:8px; }
ul.actions { list-style:none; margin:6px 0 0; padding:0; font:12px/1.5 ui-monospace,Consolas,monospace; }
ul.actions li { padding:2px 0; border-bottom:1px solid var(--rule); overflow-wrap:anywhere; } ul.actions li.failing { color:var(--fail); }
ul.actions .err, ul.actions .log { display:block; padding-left:14px; } ul.actions .log { color:var(--dim); }
.history { display:flex; align-items:center; gap:10px; flex-wrap:wrap; font-size:13px; }
.strip { display:inline-flex; gap:2px; } .blk { width:9px; height:14px; display:inline-block; background:var(--rule); }
.blk.passed, .blk.xpassed { background:var(--pass); opacity:.55; } .blk.failed, .blk.error { background:var(--fail); } .blk.skipped, .blk.xfailed { background:var(--skip); opacity:.6; }
.chip.flaky { color:var(--skip); }
#close { display:none; }
@media (max-width:1100px) {
  .wrap { grid-template-columns:1fr; } main { padding:18px 16px 60px; }
  aside { position:fixed; inset:0; height:auto; border-left:0; background:var(--bg); display:none; z-index:5; }
  aside.open { display:block; }
  #close { display:block; all:unset; cursor:pointer; position:fixed; top:10px; right:14px; z-index:6; border:1.5px solid var(--ink); padding:3px 10px; font-size:12.5px; background:var(--bg); }
  nav input { width:100%; margin-left:0; }
}
'''

RUN_PAGE_JS = r'''
(function () {
  var $ = function (s) { return document.querySelector(s); };
  var all = function (s) { return Array.prototype.slice.call(document.querySelectorAll(s)); };
  var body = document.body, pane = $('#pane');
  var filter = body.getAttribute('data-first'), q = '', tab = 0, showAll = false, current = 't-run';
  var WORDS = {fail: 'failed', skip: 'were skipped', unkn: 'have no recorded outcome'};
  var h24 = false;
  try { h24 = localStorage.getItem('clock') === '24'; } catch (err) { /* a private window: the 12 hour clock stays */ }

  function when(utc, short) {
    var d = new Date(utc);
    if (isNaN(d)) { return null; }
    var zone = h24 ? body.getAttribute('data-zone24') : body.getAttribute('data-zone12');
    var text = new Intl.DateTimeFormat('en-US', {timeZone: zone, month: 'short', day: 'numeric', year: short ? undefined : 'numeric',
      hour: h24 ? '2-digit' : 'numeric', minute: '2-digit', hour12: !h24}).format(d);
    var name = h24 ? body.getAttribute('data-zone24-name') : new Intl.DateTimeFormat('en-US', {timeZone: zone, timeZoneName: 'short'})
      .formatToParts(d).filter(function (p) { return p.type === 'timeZoneName'; })[0].value;
    return short ? text : text + ' ' + name;
  }
  function clock() {
    var any = false;
    all('time[data-utc]').forEach(function (t) { var s = when(t.getAttribute('data-utc'), t.hasAttribute('data-short')); if (s) { t.textContent = s; any = true; } });
    $('#clock').textContent = any ? (h24 ? '24 h' : '12 h') : '';
  }
  function show(id) {
    var tpl = document.getElementById(id);
    if (!tpl) { return; }
    current = id;
    pane.innerHTML = '<button type="button" id="close">Close</button>' + tpl.innerHTML;
    var bars = pane.querySelectorAll('[data-tab]');
    // the tab stays when the next test opens: a reader who compares screenshots stays on Evidence
    if (bars.length) { if (tab >= bars.length) { tab = 0; } tabs(); }
    all('.sel').forEach(function (x) { x.classList.remove('sel'); });
    all('.panelinks button').forEach(function (b) { b.classList.toggle('on', b.getAttribute('data-pane') === id); });
    if (id.charAt(1) !== '-') { all('[data-id="' + id.slice(1) + '"]').forEach(function (x) { x.classList.add('sel'); }); }
    pane.scrollTop = 0;
    clock();
  }
  function tabs() {
    all('#pane [data-tab]').forEach(function (b) { b.classList.toggle('on', Number(b.getAttribute('data-tab')) === tab); });
    all('#pane [data-tab-body]').forEach(function (s) { s.classList.toggle('on', Number(s.getAttribute('data-tab-body')) === tab); });
  }
  function list() {
    var shown = 0;
    all('#list .row').forEach(function (r) {
      var hit = (filter === 'all' || r.getAttribute('data-o') === filter) && (!q || r.getAttribute('data-test').indexOf(q) !== -1);
      r.classList.toggle('off', !hit); if (hit) { shown++; }
    });
    all('#list .modrow').forEach(function (m) {
      var n = m.nextElementSibling, any = false;
      while (n && !n.classList.contains('modrow')) { if (!n.classList.contains('off')) { any = true; } n = n.nextElementSibling; }
      m.classList.toggle('off', !any);
    });
    $('#list-h').textContent = 'Tests: ' + shown;
    var causes = $('#causes');
    if (causes) { causes.classList.toggle('off', !((filter === 'fail' || filter === 'all') && !q)); }
  }
  function map() {
    if (!$('#map')) { return; }
    var shown = 0, total = 0, narrow = WORDS[filter];
    all('#map tbody tr:not(.mod)').forEach(function (tr) {
      total++;
      var on = showAll || !narrow || tr.querySelector('.g.' + filter);
      tr.classList.toggle('off', !on); if (on) { shown++; }
    });
    all('#map tr.mod').forEach(function (m) {
      var n = m.nextElementSibling, any = false;
      while (n && !n.classList.contains('mod')) { if (!n.classList.contains('off')) { any = true; } n = n.nextElementSibling; }
      m.classList.toggle('off', !any);
    });
    $('#mapcount').textContent = shown < total ? 'Showing the ' + shown + ' tests that ' + narrow + ', of ' + total + '.' : 'Showing all ' + total + ' tests.';
    $('#maptoggle').textContent = shown < total ? 'Show all ' + total : (showAll && narrow ? 'Show only the tests that ' + narrow : '');
  }
  function setFilter(f) {
    filter = f; showAll = false;
    all('nav button').forEach(function (b) { b.classList.toggle('on', b.getAttribute('data-f') === f); });
    list(); map();
  }
  function act(target) {
    var el;
    if (target.closest('#close')) { pane.classList.remove('open'); return true; }
    if (target.closest('#clock')) { h24 = !h24; try { localStorage.setItem('clock', h24 ? '24' : '12'); } catch (err) { /* not kept */ } clock(); return true; }
    if (target.closest('#maptoggle')) { showAll = !showAll; map(); return true; }
    if ((el = target.closest('[data-pane]'))) { show(el.getAttribute('data-pane')); pane.classList.add('open'); return true; }
    if ((el = target.closest('nav button'))) { setFilter(el.getAttribute('data-f')); return true; }
    if ((el = target.closest('[data-tab]'))) { tab = Number(el.getAttribute('data-tab')); tabs(); return true; }
    if ((el = target.closest('[data-id]'))) { show('t' + el.getAttribute('data-id')); pane.classList.add('open'); return true; }
    return false;
  }
  document.addEventListener('click', function (ev) { act(ev.target); });
  document.addEventListener('keydown', function (ev) {
    if (ev.key === 'Escape') { show('t-run'); pane.classList.remove('open'); }
    if ((ev.key === 'Enter' || ev.key === ' ') && ev.target.matches('[role="button"]')) { ev.preventDefault(); act(ev.target); }
  });
  var table = $('#map table');
  if (table) {
    table.addEventListener('mouseover', function (ev) {
      var c = ev.target.closest('[data-col]');
      all('.hot').forEach(function (x) { x.classList.remove('hot'); });
      if (c) { all('[data-col="' + c.getAttribute('data-col') + '"]').forEach(function (x) { x.classList.add('hot'); }); }
    });
  }
  $('#filter').addEventListener('input', function (ev) { q = ev.target.value.trim().toLowerCase(); list(); });
  list(); map(); show('t-run');
  // a link to one test (#<its safe name>) opens that test under its tab
  var hash = '';
  try { hash = decodeURIComponent(location.hash.slice(1)); } catch (err) { /* a hash that is not valid text opens nothing */ }
  if (hash === 'advisories') { show('t-notes'); pane.classList.add('open'); }
  var linked = hash ? document.getElementById(hash) : null;
  if (linked && linked.hasAttribute('data-id')) { setFilter(linked.getAttribute('data-o')); show('t' + linked.getAttribute('data-id')); linked.scrollIntoView({block: 'center'}); }
})();
'''
