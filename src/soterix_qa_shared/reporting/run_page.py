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
from soterix_qa_shared.reporting.run_report import _OUTCOME_RANK

if TYPE_CHECKING:
    from soterix_qa_shared.reporting.run_report import RunLinks, TestRecord

# The 2 views of the clock switch: the team's own time on the 12 hour clock, and India
# time on the 24 hour clock (Justin, 09/21/2026). The zone is named in both views: people
# in 2 countries read this page, and a bare "15:55" does not say whose clock it is.
ZONE_12H = 'America/Chicago'
ZONE_24H = ('Asia/Kolkata', 'IST')

BROWSERS = ('chromium', 'firefox', 'webkit', 'chrome', 'msedge')
MAP_MAX_COLUMNS = 40

# What a reader does when the trace is not in the archive. deploy/spartan1/README.md holds the full steps.
TRACE_RECIPE = ('Ask the QA lead for it, or copy it from the machine and open it with '
                'python -m playwright show-trace. Traces are kept for a few days.')
_FOLDER_STAMP_RE = re.compile(r'^(\d{4}-\d{2}-\d{2})[ _T](\d{2})-(\d{2})-(\d{2})$')
_UTC_OFFSET_RE = re.compile(r'^([+-])([01]\d|2[0-3]):?([0-5]\d)$')

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
    offset = str(utc_offset or '').strip()
    offset = '+00:00' if offset.upper() in ('Z', 'UTC') else offset
    # '' for each time that cannot be placed, as the docstring says: a year 1 or 9999, a time
    # near 1970 on Windows, and an offset out of range each raised here and cost the run its page
    try:
        local = datetime.fromisoformat(text.replace(' ', 'T'))
        if local.tzinfo is None:
            m = _UTC_OFFSET_RE.match(offset)
            if m:
                delta = timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
                local = local.replace(tzinfo=timezone(delta if m.group(1) == '+' else -delta))
            else:
                local = local.astimezone()
        # isoformat, not strftime: %Y gives '1' for the year 1 on Linux and '0001' on Windows
        return local.astimezone(UTC).replace(tzinfo=None).isoformat(timespec='seconds') + 'Z'
    except (ValueError, OSError, OverflowError):
        return ''


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
    # a serial ('q300c5b44') is named by its end, as people do; a word ('person') stays whole
    short = tail[-4:] if len(tail) > 8 and any(ch.isdigit() for ch in tail) else tail
    return {'group': head.upper(), 'label': short, 'title': param}


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
            cells = rows.setdefault(function_of(rec.nodeid), {})
            # A cell can be claimed twice: 2 browsers, or 1 node id spelled with and without its
            # xdist '@group' in 2 of the sources. Returning None here removed the whole map with no
            # word to the reader (audit 09/22/2026). The worse outcome wins the square.
            if param not in cells or _OUTCOME_RANK.get(rec.outcome, 0) > _OUTCOME_RANK.get(cells[param].outcome, 0):
                cells[param] = rec
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
            meta.update({k: str(named[param][k]) for k in ('group', 'label', 'title') if named[param].get(k)})
        columns.append({'param': param, **meta})
    columns.sort(key=lambda c: (c['group'], [int(n) for n in re.findall(r'\d+', c['label'])] or [0], c['label'], c['param']))
    ordered = sorted(rows.items(), key=lambda kv: (next(iter(kv[1].values())).module_name, kv[0]))
    noun = given.get('noun')
    return {'noun': noun if isinstance(noun, str) and noun else 'variant', 'columns': columns,
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
                   axis: dict | None, with_product: bool, has_run_log: bool = False,
                   published_traces: bool = True) -> str:
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
    else:
        evidence.append(_lab('Screenshots') + '<p class="sub">No screenshot for this test.</p>')
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
        evidence.append(_lab('Video') + f'<video controls preload="none" src="{_href(rel)}"></video>'
                        f'<div><a href="{_href(rel)}">Video clip</a></div>')
    if failed and not rec.videos:
        evidence.append(_lab('Video') + '<p class="sub">No video kept for this test.</p>')
    if failed and not rec.traces:
        evidence.append(_lab('Trace') + '<p class="sub">No trace kept for this test.</p>')
    for rel in rec.traces:
        label = 'Trace zip' if rel.lower().endswith('.zip') else Path(rel).name
        if published_traces:
            evidence.append(_lab('Trace') + f'<div><a href="{_href(rel)}">{escape(label)}</a></div>')
            if rel.lower().endswith('.zip'):
                evidence.append(f'<pre>python -m playwright show-trace "{escape(os.path.abspath(run_dir / rel))}"</pre>')
        else:
            # The nightly copies a run to the served directory without its zips, so a link here
            # would 404 for every reader (audit 09/22/2026). Say where the trace is instead.
            evidence.append(_lab('Trace') + f'<p class="sub">The trace of this test is kept on the machine that ran it, '
                            f'not in this archive: <code>{escape(run_dir.name)}/{escape(rel)}</code>. '
                            f'{escape(TRACE_RECIPE)}</p>')
    if rec.log_file:
        evidence.append(_lab('Captured output') + f'<div><a href="{_href(rec.log_file)}">{escape(Path(rec.log_file).name)}</a></div>')
    if not failed and has_run_log:
        evidence.append(_lab('Captured output') + '<p class="sub">The captured output of passed and skipped tests is in '
                        '<a href="log/run.log">log/run.log</a>, one section per test.</p>')
    if not evidence:
        evidence.append('<p class="sub">This run kept no screenshot, trace or log for this test.</p>')

    # --- History
    history = []
    strip = render_strip(rec.history, '../', links=links, clock=True)
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
        ('Run', [('Outcome', escape(exit_outcome(summary.get('exitstatus')))),
                 ('Exit code', escape(str(summary.get('exitstatus'))) if summary.get('exitstatus') is not None else ''),
                 ('Product', escape(product)),
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
             summary: Mapping[str, Any], unknown: int = 0, with_changes: bool = True) -> tuple[str, str]:
    """The large line and the line under it."""
    from soterix_qa_shared.reporting.run_report import (
        DELTA_FIXED,
        DELTA_NEW_FAILURE,
        DELTA_STILL_FAILING,
        PASSED_OUTCOMES,
        plural,
    )
    total = failed + passed + skipped + unknown   # each row of the list: the tabs and the verdict agree
    if total == 0:
        return '<em class="tone-skip">No tests ran.</em>', ''
    if failed:
        big = (f'<em class="tone-fail">{failed} of {total} failed.</em> '
               f'{plural(causes, "cause", "causes")}.')
    elif skipped or unknown:
        big = f'<em class="tone-pass">{passed} of {total} passed.</em>'
    else:
        big = f'<em class="tone-pass">All {total} passed.</em>'
    labels = [r.delta for r in records]
    parts = []
    counts = [p for p in (f'{passed} passed' if failed and passed else '', f'{skipped} skipped' if skipped else '') if p]
    if counts:
        parts.append(', '.join(counts) + '.')
    errors = sum(1 for r in records if r.outcome == 'error')
    if errors:
        parts.append(plural(errors, 'failure is an error', 'failures are errors') + ': the test could not start.')
    if unknown:
        parts.append(plural(unknown, 'test has', 'tests have') + ' no recorded outcome.')
    changes = [(labels.count(DELTA_NEW_FAILURE), 'new failure', 'new failures'),
               (labels.count(DELTA_STILL_FAILING), 'still failing', 'still failing'),
               (labels.count(DELTA_FIXED), 'fixed', 'fixed')]
    detail = ', '.join(plural(n, one, many) for n, one, many in changes if n)
    if detail and with_changes:
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
        DELTA_FIXED,
        DELTA_NEW_FAILURE,
        DELTA_NEW_TEST,
        DELTA_STILL_FAILING,
        FAILURE_OUTCOMES,
        SLOWEST_COUNT,
        RunLinks,
        delta_wording,
        duration_text,
        first_line,
        friendly_stamp,
        friendly_time,
        group_failures,
        outcome_label,
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
    # with a previous run the change counts are the row of filters under the tabs, not a sentence
    big, sub = _verdict(counts['fail'], counts['pass'], counts['skip'], len(groups), records, summary, counts['unkn'],
                        with_changes=not previous)

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
            seen = 'last seen ' + time_html(lead.seen.get('started'), lead.seen.get('utc_offset'),
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
                tds.append(f'<td{edge} data-col="{i}" data-id="{ids[rec.nodeid]}"><button type="button" class="g {bucket(rec.outcome)}" '
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
    # QA_TRACES_PUBLISHED=0 when the run will be copied somewhere without its zips (the nightly does).
    published_traces = (os.environ.get('QA_TRACES_PUBLISHED', '1') or '1').strip().lower() not in ('0', 'off', 'false', 'no')
    rows, templates, last_module = [], [], None
    used_ids: set[str] = set()
    for rec in records:
        row_id, n = rec.safe, ids[rec.nodeid]
        while row_id in used_ids:   # a safe name that repeats, and a suffix that is itself a safe name
            row_id, n = f'{rec.safe}-{n}', n + 1
        used_ids.add(row_id)
        if rec.module_name != last_module:
            # a module is a group that folds: 370 passed tests were 1 long column (Justin, 09/21/2026)
            rows.append(('</details>' if last_module is not None else '')
                        + f'<details class="modgroup" open><summary class="modrow">{escape(rec.module_name)} <b></b></summary>')
            last_module = rec.module_name
        key = ' '.join(w for w in (rec.display_title, rec.nodeid, rec.case_key, rec.case_title, rec.module_name,
                                   rec.outcome, rec.delta, rec.product, column_name(axis, rec.nodeid)) if w).lower()
        right = ' · '.join(part for part in (rec.product if with_product else '', column_name(axis, rec.nodeid)) if part)
        rows.append(
            f'<div class="row" role="button" tabindex="0" id="{escape(row_id, quote=True)}" data-id="{ids[rec.nodeid]}" '
            f'data-o="{bucket(rec.outcome)}" data-d="{escape(rec.delta, quote=True)}" data-test="{escape(key, quote=True)}"><span class="g {bucket(rec.outcome)}" role="img" '
            f'aria-label="{escape(outcome_label(rec.outcome), quote=True)}"></span>'
            f'<span class="t">{escape(rec.display_title)}</span><span class="m">{escape(rec.delta)}</span>'
            f'<span class="m">{escape(right)}</span><span class="m">{escape(rec.duration_text)}</span></div>')
        templates.append(_test_template(ids[rec.nodeid], rec, run_dir, links, axis, with_product, has_run_log,
                                        published_traces))

    if last_module is not None:
        rows.append('</details>')

    # --- the changes since the previous run, as filters. The link to the previous run only opened
    # that run: it did not say what changed (Justin, 09/21/2026).
    changes_html = ''
    if previous:
        labels = [r.delta for r in records]
        buttons = ''.join(
            f'<button type="button" data-d="{escape(label, quote=True)}">{escape(text)}<b>{labels.count(label)}</b></button>'
            for label, text in ((DELTA_NEW_FAILURE, 'New failures'), (DELTA_STILL_FAILING, 'Still failing'),
                                (DELTA_FIXED, 'Fixed'), (DELTA_NEW_TEST, 'New tests')) if labels.count(label))
        since = time_html(previous.get('started'), previous.get('utc_offset'), friendly_time(previous.get('started')), short=True)
        opened = f'<a href="{run_href(previous["name"], runs_dir, "../")}">Open that run</a>'
        changes_html = (f'<div id="changes"><span class="lab">Since the previous run, {since}</span>'
                        + (buttons or '<span class="sub">No test changed.</span>') + opened + '</div>')

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
        compared = (f'The labels New failure, Still failing, Fixed and New test compare this run with the '
                    f'<a href="{run_href(previous["name"], runs_dir, "../")}">previous run of these '
                    f'tests</a>, {time_html(previous.get("started"), previous.get("utc_offset"), friendly_time(previous.get("started")), short=True)}.')
    else:
        compared = escape(delta_wording(None))
    advisories = [a for a in (summary.get('advisories') or []) if isinstance(a, dict)]
    run_template = _run_template(summary, run_dir, product, target, duration, str(started), file_links, compared, unattached)
    templates.append(run_template)
    pane_at_rest = run_template[len('<template id="t-run">'):-len('</template>')]
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

    heading = f'<span class="product">{escape(product)}</span>' if product else ''
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
<nav>{nav}<input id="filter" type="search" placeholder="Filter tests: title, case key, module" title="Press / to type here"
 aria-label="Filter tests"></nav>
{changes_html}
{causes_block}
{map_block}
<div><h2><span id="list-h">Tests</span><span class="listtools"><button type="button" data-fold="open">Expand all</button><button type="button" data-fold="shut">Collapse all</button></span></h2>
<p id="nomatch" class="sub off"><span id="nomatch-text"></span> <button type="button" id="elsewhere"></button></p>
<div id="list">{"".join(rows)}</div></div>
</main><aside id="pane">{pane_at_rest}</aside></div>
{"".join(templates)}
<script>{RUN_PAGE_JS}</script>
</body>
</html>
'''


RUN_PAGE_CSS = '''
:root { --ink:#1b1d21; --dim:#5f646c; --rule:#d9dbe0; --bg:#fbfbfa; --fail:#b3261e; --skip:#9a7b1c; --pass:#2f7d4f; --sel:#efeee9; }
@media (prefers-color-scheme: dark) { :root { --ink:#e8e8e6; --dim:#9a9ea6; --rule:#33363c; --bg:#16171a; --fail:#ff8a80; --skip:#d9b64a; --pass:#6fcf97; --sel:#22242a; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.45 "Segoe UI",system-ui,sans-serif; }
a { color:inherit; }
.wrap { display:grid; grid-template-columns:minmax(0,1fr) 460px; min-height:100vh; }
main { padding:24px 32px 80px; min-width:0; }
aside { border-left:1px solid var(--rule); padding:28px 24px 60px; position:sticky; top:0; height:100vh; overflow:auto; }
.brand-row { display:flex; align-items:center; gap:12px; margin:0 0 8px; }
.brand { display:block; }
.product { display:block; font-size:24px; line-height:1.2; letter-spacing:0; text-transform:none; color:var(--ink); font-weight:700; margin:0 0 3px; }
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
nav button:focus-visible, .panelinks button:focus-visible, .tabs button:focus-visible, #clock:focus-visible,
#maptoggle:focus-visible, #close:focus-visible, #filter:focus-visible, #changes button:focus-visible,
.listtools button:focus-visible, #elsewhere:focus-visible, .modrow:focus-visible { outline:2px solid var(--ink); outline-offset:2px; }
.chip.flaky { color:var(--ink); font-weight:600; }
body.noscroll { overflow:hidden; }
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
tbody td { padding:0; text-align:center; min-width:52px; height:22px; } tbody td[data-id] { cursor:pointer; } td .g { margin:0; cursor:pointer; }
td .g:hover, td .g.sel, td .g:focus-visible { outline:2px solid var(--ink); outline-offset:2px; background-clip:padding-box; }
tr.mod td { font:11px/1 ui-monospace,Consolas,monospace; color:var(--dim); padding:14px 0 4px; text-align:left; }
.modgroup { border-bottom:1px solid var(--rule); } .modgroup[open] { border-bottom:0; }
.modrow { font:12px/1 ui-monospace,Consolas,monospace; color:var(--dim); padding:9px 0; cursor:pointer; list-style:none; display:flex; gap:10px; align-items:baseline; }
.modrow::-webkit-details-marker { display:none; } .modrow::before { content:'+'; width:10px; color:var(--ink); } .modgroup[open] > .modrow::before { content:'-'; }
.modrow b { color:var(--ink); font-weight:600; } .modrow:hover { color:var(--ink); }
h2 { display:flex; align-items:baseline; gap:16px; } .listtools { margin-left:auto; display:flex; gap:14px; }
.listtools button, #elsewhere { all:unset; cursor:pointer; font-size:12px; letter-spacing:0; text-transform:none; font-weight:400; color:var(--dim); border-bottom:1px dotted var(--dim); }
.listtools button:hover, #elsewhere:hover { color:var(--ink); } #elsewhere:empty { display:none; } #elsewhere { color:var(--ink); font-size:13.5px; border-bottom:1.5px solid var(--ink); }
#nomatch { margin:10px 0 0; font-size:14.5px; color:var(--ink); }
#changes { display:flex; gap:20px; align-items:baseline; flex-wrap:wrap; padding:9px 0 0; font-size:13px; } #changes .lab { display:inline; }
#changes button { all:unset; cursor:pointer; color:var(--dim); border-bottom:2px solid transparent; padding-bottom:2px; } #changes button b { color:var(--ink); margin-left:4px; }
#changes button.on { color:var(--ink); border-bottom-color:var(--ink); } #changes a { margin-left:auto; color:var(--dim); }

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
  var filter = body.getAttribute('data-first'), q = '', delta = '', tab = 0, showAll = false, current = 't-run';
  var fold = null;   // null = the default for this list's length; true/false = what the reader chose
  var NAMES = {fail: 'Failed', skip: 'Skipped', pass: 'Passed', unkn: 'Unknown', all: 'All'};
  var FOLD_ABOVE = 40;   // a list longer than this opens with its modules folded
  var WORDS = {fail: 'failed', skip: 'were skipped', pass: 'passed', unkn: 'have no recorded outcome'};
  var h24 = false, opener = null;
  var baseTitle = document.title.replace(/Test Run - .*$/, 'Test Run - ');
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
    // the tooltips of a history strip, and the tab's title, are times too
    all('#pane .blk[data-utc]').forEach(function (b) { var s = when(b.getAttribute('data-utc'), true); if (s) { b.title = s + ': ' + b.getAttribute('data-label'); } });
    var head = $('h1 time[data-utc]'), stamp = head ? when(head.getAttribute('data-utc'), false) : null;
    if (stamp) { document.title = baseTitle + stamp; }
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
    all('#pane [data-tab]').forEach(function (b) { var on = Number(b.getAttribute('data-tab')) === tab; b.classList.toggle('on', on); b.setAttribute('aria-pressed', on); });
    all('#pane [data-tab-body]').forEach(function (s) { s.classList.toggle('on', Number(s.getAttribute('data-tab-body')) === tab); });
  }
  function matches(r, anyTab) {
    var inTab = delta ? r.getAttribute('data-d') === delta : (anyTab || filter === 'all' || r.getAttribute('data-o') === filter);
    return inTab && (!q || r.getAttribute('data-test').indexOf(q) !== -1);
  }
  function list() {
    var shown = 0, elsewhere = 0, inTab = 0;
    all('#list .row').forEach(function (r) {
      var hit = matches(r, false);
      r.classList.toggle('off', !hit); if (hit) { shown++; } else if (q && !delta && matches(r, true)) { elsewhere++; }
      if (delta ? r.getAttribute('data-d') === delta : (filter === 'all' || r.getAttribute('data-o') === filter)) { inTab++; }
    });
    // A module is a group that folds. A short list opens whole; a long one opens folded, each module
    // with its count. `fold` holds a choice the reader made with Expand all or Collapse all, and it
    // survives until the tab or the typed word changes (audit 09/22/2026: every keystroke undid it).
    var open = fold === null ? (!!q || shown <= FOLD_ABOVE) : fold;
    all('#list .modgroup').forEach(function (g) {
      var n = g.querySelectorAll('.row:not(.off)').length;
      g.classList.toggle('off', !n); g.querySelector('summary b').textContent = n;
      if (fold !== null || g.open !== open) { g.open = open; }
    });
    $('#list-h').textContent = q ? 'Tests: ' + shown + ' of ' + inTab : 'Tests: ' + shown;
    document.querySelector('.listtools').classList.toggle('off', shown === 0);
    // say so when nothing is found, and where the word does match
    var where = delta || NAMES[filter];
    var text = '';
    if (!shown && q) { text = 'No test under ' + where + ' matches "' + q + '".'; }
    else if (!shown) { text = 'No test under ' + where + ' in this run.'; }
    $('#nomatch-text').textContent = text;
    $('#elsewhere').textContent = (!shown && elsewhere) ? elsewhere + (elsewhere === 1 ? ' test matches' : ' tests match') + ' under All: show' : '';
    $('#nomatch').classList.toggle('off', !!shown);
    var causes = $('#causes');
    if (causes) { causes.classList.toggle('off', !((filter === 'fail' || filter === 'all') && !q && !delta)); }
  }
  function map() {
    if (!$('#map')) { return; }
    var shown = 0, total = 0, narrow = (q || delta) ? 'match' : WORDS[filter], visible = {};
    all('#list .row:not(.off)').forEach(function (r) { visible[r.getAttribute('data-id')] = true; });
    all('#map tbody tr:not(.mod)').forEach(function (tr) {
      total++;
      var on = showAll || !narrow;
      if (!on) { var cells = tr.querySelectorAll('button[data-id]'); for (var i = 0; i < cells.length && !on; i++) { on = !!visible[cells[i].getAttribute('data-id')]; } }
      tr.classList.toggle('off', !on); if (on) { shown++; }
    });
    all('#map tr.mod').forEach(function (m) {
      var n = m.nextElementSibling, any = false;
      while (n && !n.classList.contains('mod')) { if (!n.classList.contains('off')) { any = true; } n = n.nextElementSibling; }
      m.classList.toggle('off', !any);
    });
    // 1 test 'matches', 'was skipped', 'has no recorded outcome'; 'failed' and 'passed' are the same for 1 and for many
    var one = {match: 'matches', 'were skipped': 'was skipped', 'have no recorded outcome': 'has no recorded outcome'};
    var said = shown === 1 ? 'the 1 test that ' + (one[narrow] || narrow) : 'the ' + shown + ' tests that ' + narrow;
    $('#mapcount').textContent = shown < total ? 'Showing ' + said + ', of ' + total + '.' : 'Showing all ' + total + ' tests.';
    $('#maptoggle').textContent = shown < total ? 'Show all ' + total : (showAll && narrow ? 'Show only the tests that ' + narrow : '');
  }
  function setDelta(d) {   // the tests with 1 change label, whatever their outcome
    if (d !== delta) { fold = null; }
    delta = d; filter = 'all'; showAll = false;
    all('nav button').forEach(function (b) { b.classList.remove('on'); b.setAttribute('aria-pressed', false); });
    all('#changes button').forEach(function (b) { var on = b.getAttribute('data-d') === d; b.classList.toggle('on', on); b.setAttribute('aria-pressed', on); });
    list(); map();
  }
  function setFilter(f) {
    if (f !== filter || delta) { fold = null; }   // a new view returns to the default; the same tab keeps the reader's choice
    filter = f; delta = ''; showAll = false;
    all('#changes button').forEach(function (b) { b.classList.remove('on'); b.setAttribute('aria-pressed', false); });
    all('nav button').forEach(function (b) { var on = b.getAttribute('data-f') === f; b.classList.toggle('on', on); b.setAttribute('aria-pressed', on); });
    list(); map();
  }
  // On a narrow screen the pane covers the page: the page behind does not scroll, the focus goes
  // to Close, and it goes back to what opened the pane.
  function openPane(from) {
    pane.classList.add('open');
    if (getComputedStyle(pane).position === 'fixed') {
      opener = from || null; body.classList.add('noscroll');
      var toClose = function () { var c = $('#close'); if (c && pane.classList.contains('open')) { c.focus(); } };
      toClose();
      // On a link to a test the browser puts the focus on the row AFTER this script ran, behind the pane.
      if (document.readyState !== 'complete') { window.addEventListener('load', function () { setTimeout(toClose, 0); }, {once: true}); }
    }
  }
  function closePane() {
    pane.classList.remove('open'); body.classList.remove('noscroll');
    if (opener && document.contains(opener)) { opener.focus(); } opener = null;
  }
  function act(target) {
    var el;
    if (target.closest('#close')) { closePane(); return true; }
    if (target.closest('#clock')) { h24 = !h24; try { localStorage.setItem('clock', h24 ? '24' : '12'); } catch (err) { /* not kept */ } clock(); return true; }
    if (target.closest('#maptoggle')) { showAll = !showAll; map(); return true; }
    if ((el = target.closest('[data-pane]'))) { show(el.getAttribute('data-pane')); openPane(el); return true; }
    if ((el = target.closest('nav button'))) { setFilter(el.getAttribute('data-f')); return true; }
    if ((el = target.closest('#changes button'))) { if (delta === el.getAttribute('data-d')) { setFilter(body.getAttribute('data-first')); } else { setDelta(el.getAttribute('data-d')); } return true; }
    if (target.closest('#elsewhere')) { setFilter('all'); return true; }
    if ((el = target.closest('[data-fold]'))) { fold = el.getAttribute('data-fold') === 'open';
      all('#list .modgroup').forEach(function (g) { g.open = fold; }); return true; }
    if ((el = target.closest('[data-tab]'))) { tab = Number(el.getAttribute('data-tab')); tabs(); return true; }
    if ((el = target.closest('[data-id]'))) { show('t' + el.getAttribute('data-id')); openPane(el); return true; }
    return false;
  }
  document.addEventListener('click', function (ev) { act(ev.target); });
  document.addEventListener('keydown', function (ev) {
    // Escape in a filter box with text clears the box (the browser does it) and keeps the open test
    if (ev.key === 'Escape' && !(ev.target.id === 'filter' && ev.target.value)) { show('t-run'); closePane(); }
    if ((ev.key === 'Enter' || ev.key === ' ') && ev.target.matches('[role="button"]')) { ev.preventDefault(); act(ev.target); }
    if (ev.key === '/' && !ev.target.matches('input, textarea')) { ev.preventDefault(); $('#filter').focus(); }
  });
  var table = $('#map table');
  if (table) {
    table.addEventListener('mouseover', function (ev) {
      var c = ev.target.closest('[data-col]');
      all('.hot').forEach(function (x) { x.classList.remove('hot'); });
      if (c) { all('[data-col="' + c.getAttribute('data-col') + '"]').forEach(function (x) { x.classList.add('hot'); }); }
    });
  }
  $('#filter').addEventListener('input', function (ev) { q = ev.target.value.trim().toLowerCase(); fold = null; list(); map(); });
  setFilter(filter); show('t-run');   // setFilter, not list(): the open tab says aria-pressed from the start
  // a link to one test (#<its row id>) opens that test under its tab, and #advisories opens the notes
  function follow() {
    var hash = '';
    try { hash = decodeURIComponent(location.hash.slice(1)); } catch (err) { /* a hash that is not valid text opens nothing */ }
    if (hash === 'advisories') { show('t-notes'); openPane(null); }
    var linked = hash ? document.getElementById(hash) : null;
    if (linked && linked.classList.contains('row')) {
      setFilter(linked.getAttribute('data-o')); linked.closest('details').open = true;
      show('t' + linked.getAttribute('data-id')); openPane(linked); linked.scrollIntoView({block: 'center'});
    }
  }
  follow();
  window.addEventListener('hashchange', follow);
})();
'''
