"""Per-run HTML report -- builder and CLI.

Turns one archived run folder (see run_archive) into run.html: a page that
puts every piece of the run's evidence one click away, failures first.
Consumers invoke it through their plugins/run_report.py shim:

    python -m plugins.run_report latest
    python -m plugins.run_report reports/runs/<start>__<target>
    python -m plugins.run_report --all      # every archived run without a run.html

What the page shows, in order:
  - Verdict: one sentence in plain words, e.g. "3 of 49 tests failed - 2
    new failures, 1 still failing; 1 test skipped." or "All 49 tests
    passed."
  - Header: the product under test (plugins/products.py: also the page
    title's prefix, "NexaiQ-Core - Test Run - ..."), target, start time,
    duration, outcome counts, the run's outcome (pytest's exit status as
    a word: Passed, Failed, Interrupted, Error, No tests, with the code
    as a tooltip), provenance (Env, host, git commit, browser, Playwright
    versions), and links: Raw pytest-html,
    Archive Index, Health when health.html exists, the previous run of
    these tests (which the change labels compare against), and Folder.
    When a run mixes products, every test shows its own.
  - A filter box: type part of a title, node id, case key, module, or
    outcome and only the matching tests stay visible.
  - Failures: one card per failed or errored test with the error message,
    the failing Playwright action and its call log, the actions that led
    up to it, console errors, every screenshot of the test (final plus
    mid-drag frames, click for full size), the video clip when one was
    kept, and the exact show-trace command for the trace zip. Tests that
    failed with the same error (first line, with numbers, quotes, hex
    addresses, and locator text reduced) sit together under "N tests
    share this error". Every failure says whether its error is new or
    was seen in one of the previous 20 runs of these tests, and when.
  - Notes: the [VIEWS] / [LPR] / [EXPORT] advisory lines from
    summary.json. The heading keeps the id "advisories", which the health
    page links.
  - Slowest 5: the longest tests of the run.
  - Passed and Skipped: collapsed lists grouped by module, each test with
    its final screenshot and its advisory lines. Mid-drag frames stay on
    disk and show only on a failure card.

Every test shows its readable title first (the docstring's first line, or
the function name as words), the QA Touch case title beneath it when the
test is mapped, and the node id smaller under both (a passed or skipped
row shows the short id, the full one clipped beside it). The node id stays the
join key for the QA Touch mapping and the archive history, so the page
never renames a test. Each test also carries its duration, a history
strip (its outcome in each of the last 20 archived runs of these tests,
oldest left, with "passed 17/20" and a flaky label when it both passed
and failed in that window), and a change label against the previous run:
New failure, Fixed, Still failing, or New test. Failed tests link their
captured output under log/. The output of every other test is in
log/run.log.

Where the facts come from, most reliable first:
  1. summary.json "tests" - run_archive's own per-test record: title,
     module, QA Touch case, outcome, per-phase timing, log file, and the
     error line of a failure.
  2. report.html - pytest-html embeds every test's outcome, duration, and
     captured log (the traceback) as JSON in a data-jsonblob attribute.
  3. last_run_results.json - qatouch_reporter's per-nodeid outcomes. Only
     present when that reporter ran (--qatouch-upload), so never assumed.
  4. The file names themselves - a __FAILED screenshot or a -trace.zip
     names its test even when neither file above exists.
  Files attach to tests through the same sanitizer report_media and
  conftest use to name them. Names that run_archive shortened to fit the
  path cap (<head>-<8-hex-digest>-<tail>) still match: the digest is
  recomputed from the expected name, with a longest-common-prefix
  fallback for names cut by an older layout.

Links to other runs (the history strips, the seen-before and previous-run
notes) go through run_href, which looks in the target folder: run.html
when it has one, else report.html, else the folder. An archive written
before run.html existed then opens something instead of a missing file.
`--all` builds the missing pages once, so those links reach run.html too.

Stdlib only, no browser, and read-only on the run folder except for the
run.html it writes. Every input is optional: a missing file or key
renders as '-' or an empty section, never as an error. An archive
written before schema 4 carries no product; the page then derives it
from the node ids the same way, less the marker the archive did not keep.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys
import zipfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import quote

from soterix_qa_shared.qatouch.mapping import canonical_nodeid
from soterix_qa_shared.reporting.branding import favicon_link, header_logo
from soterix_qa_shared.reporting.products import (
    is_mixed,
    product_for,
    product_name,
    run_product,
)

# Same sanitizer and caps as report_media._safe_name (screenshots) and
# conftest._trace_path (traces, videos): the archive names are those
# files' names, so matching must reproduce them exactly.
SAFE_NAME_MAX = 120
TRACE_NAME_MAX = 150

# Run folders are named <session-start>__<slug>. Defined here, not in
# run_archive, because that module imports this one and the previous-run
# lookup below needs the pattern too.
RUN_DIR_RE = re.compile(r'^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}__')

# How many actions before the failing one the failure card lists.
LAST_ACTIONS = 15

# How many of the run's longest tests the "Slowest" block lists.
SLOWEST_COUNT = 5

# How many archived runs of the target the history strip and the "new
# error" check look at, this run included. The archive keeps that many
# per target (run_archive.KEEP_RUNS), so the window is never wider than
# what is on disk.
HISTORY_RUNS = 20

# Change labels against the previous run of the same target, in the words
# a reader sees on the page. The CSS class is the lower-case form.
DELTA_NEW_FAILURE = 'New failure'
DELTA_FIXED = 'Fixed'
DELTA_STILL_FAILING = 'Still failing'
DELTA_NEW_TEST = 'New test'

# Error lines pytest prefixes with 'E ' in a longrepr; the first block of
# them is the headline a reader wants before the full traceback.
_ERROR_LINE_RE = re.compile(r'^E {1,3}(.*)$')
_HEADLINE_MAX = 30
# The one error line kept in summary.json per failure, capped so the
# summary stays small.
ERROR_LINE_MAX = 300

# The parts of an error line that change between runs of the same
# failure. Reduced in this order: an object repr holds a hex address, a
# locator call holds quotes and digits, a quoted value holds digits.
_SIG_OBJECT_RE = re.compile(r'<[^<>]*?object at [^<>]*>')
_SIG_LOCATOR_RE = re.compile(r'\b(locator|get_by_\w+|nth|filter)\([^()]*\)')
_SIG_QUOTED_RE = re.compile(r'"[^"]*"|\'[^\']*\'')
_SIG_HEX_RE = re.compile(r'\b0x[0-9a-fA-F]+\b')
_SIG_NUMBER_RE = re.compile(r'\d+(?:\.\d+)?')

# Start times as summary.json writes them (2026-08-24T14:27:25) and as the
# run folder names carry them (2026-08-24_14-27-25).
_STARTED_RE = re.compile(r'^(\d{4})-(\d{2})-(\d{2})[T_ ](\d{2})[:-](\d{2})(?:[:-](\d{2}))?')
_MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
           'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')

_SCREENSHOT_RE = re.compile(
    r'^(?P<test>.*?)__(?P<suffix>PASSED|FAILED|SETUP_FAILED|SKIPPED)'
    r'(?:__(?P<label>.+)-(?P<index>\d+))?$')
_ARTIFACT_RE = re.compile(r'^(?P<test>.*?)-(?P<kind>login-failure-trace|trace|video)$')
_DIGEST_RE = re.compile(r'-([0-9a-f]{8})-')

# report.html (pytest-html) and last_run_results.json (qatouch_reporter)
# each spell outcomes their own way; the page uses one vocabulary.
_HTML_OUTCOMES = {
    'passed': 'passed', 'failed': 'failed', 'error': 'error',
    'skipped': 'skipped', 'xfailed': 'xfailed', 'xpassed': 'xpassed',
}
_SUFFIX_OUTCOMES = {'PASSED': 'passed', 'FAILED': 'failed',
                    'SETUP_FAILED': 'error', 'SKIPPED': 'skipped'}
# Worse outcomes win when two sources disagree about one test.
_OUTCOME_RANK = {'error': 5, 'failed': 4, 'xfailed': 3, 'skipped': 2,
                 'xpassed': 1, 'passed': 0}
FAILURE_OUTCOMES = ('failed', 'error')
PASSED_OUTCOMES = ('passed', 'xpassed')

# How an outcome reads on the page: pytest's words, as labels.
OUTCOME_LABELS = {'passed': 'Passed', 'failed': 'Failed', 'error': 'Error', 'skipped': 'Skipped',
                  'xfailed': 'Expected failure', 'xpassed': 'Unexpected pass', 'rerun': 'Rerun',
                  'unknown': 'Unknown'}


def outcome_label(outcome: str) -> str:
    """'Passed', 'Expected failure', ... - the outcome as a reader's word."""
    return OUTCOME_LABELS.get(str(outcome), str(outcome).capitalize())
SKIPPED_OUTCOMES = ('skipped', 'xfailed')

# pytest's exit codes (ExitCode in _pytest/config) as the words a reader
# without pytest knows. 3 (internal error) and 4 (usage error) share a
# word: both mean pytest stopped before the tests could answer.
EXIT_OUTCOMES = {0: 'Passed', 1: 'Failed', 2: 'Interrupted', 3: 'Error',
                 4: 'Error', 5: 'No tests'}

# The tooltip of every "Notes" header (the index, health, run.html): the
# column counts advisory lines, and the word alone does not say so.
NOTES_TITLE = ('Warnings the page objects logged during the run '
               '(drag fallbacks, content health, export timing)')


def exit_outcome(exitstatus: object) -> str:
    """The exit status in words: Passed (0), Failed (1), Interrupted (2),
    Error (3 and 4), No tests (5). A code outside the table reads
    'Exit N', so a new pytest code is never hidden. None reads '-'."""
    if exitstatus is None:
        return '-'
    try:
        code = int(exitstatus)
    except (TypeError, ValueError):
        return str(exitstatus)
    return EXIT_OUTCOMES.get(code, f'Exit {code}')


def outcome_html(exitstatus: object) -> str:
    """The outcome word with the numeric code as its tooltip, for the
    reader who does know pytest. '-' without a status."""
    if exitstatus is None:
        return '-'
    return (f'<span title="exit code {escape(str(exitstatus))}">'
            f'{escape(exit_outcome(exitstatus))}</span>')


@dataclass
class TestRecord:
    """Everything the page knows about one test."""

    nodeid: str
    outcome: str = 'unknown'
    duration: str = ''
    log: str = ''
    screenshots: list[str] = field(default_factory=list)
    frames: list[str] = field(default_factory=list)
    traces: list[str] = field(default_factory=list)
    videos: list[str] = field(default_factory=list)
    advisories: list[str] = field(default_factory=list)
    # From summary.json "tests" (run_archive); empty on older archives.
    title: str = ''
    module: str = ''
    case_key: str = ''
    case_title: str = ''
    product: str = ''
    log_file: str = ''
    seconds: float | None = None
    delta: str = ''
    # {row, since, what} when the register already names this test's failure as an app defect
    known: dict | None = None
    # Filled by build_run_report for failures: the error headline, its
    # signature, the trace digest, and the newest earlier run that showed
    # the same signature ({'run', 'started'}, None when the error is new).
    headline: str = ''
    signature: str = ''
    digest: dict | None = None
    seen: dict | None = None
    # history_facts() over the archived runs of this target.
    history: dict = field(default_factory=dict)
    # The error's first line as summary.json recorded it: the headline when report.html gave no log
    error: str = ''
    # True when summary.json gave this test its outcome: a leftover failure
    # screenshot or trace then never overrides it (a test that passed on a
    # rerun kept its first attempt's trace and was listed under Failures).
    recorded: bool = False
    reruns: int = 0
    # Why a skipped test was skipped, and the seconds of setup, call and teardown (summary.json).
    reason: str = ''
    phases: dict = field(default_factory=dict)

    @property
    def safe(self) -> str:
        return _safe_name(self.nodeid, SAFE_NAME_MAX)

    @property
    def safe_trace(self) -> str:
        return _safe_name(self.nodeid, TRACE_NAME_MAX)

    @property
    def display_title(self) -> str:
        return self.title or readable_title(self.nodeid)

    @property
    def module_name(self) -> str:
        return self.module or module_of(self.nodeid)

    @property
    def duration_text(self) -> str:
        """Seconds from the archive when known, else pytest-html's text."""
        return format_seconds(self.seconds) if self.seconds is not None else self.duration


# ---------------------------------------------------------------- names

def _safe_name(nodeid: str, cap: int) -> str:
    """The filesystem stem the plugins derive from a node id."""
    return re.sub(r'[^\w.-]+', '-', nodeid).strip('-')[:cap]


def readable_title(nodeid: str, doc: str | None = None) -> str:
    """The title a reader sees for a test.

    The first non-empty line of the docstring when there is one, else the
    function name as words: test_drag_camera_to_tile[chromium] reads
    "Drag camera to tile". Display only - the node id stays the key.
    """
    for line in (doc or '').splitlines():
        text = line.strip()
        if text:
            return text
    # without xdist's '@<group>': with it the '[param]$' rule missed, and a test with no
    # docstring read 'Delete user[chromium]@account users' (seen 09/21/2026)
    name = canonical_nodeid(nodeid).rsplit('::', 1)[-1]
    name = re.sub(r'\[.*\]$', '', name)
    name = re.sub(r'^test_?', '', name)
    words = name.replace('_', ' ').strip()
    return (words[:1].upper() + words[1:]) if words else nodeid


# Only what pytest adds: a parameter that starts with the browser, or any parameter with the
# '@<group>' of pytest-xdist after it. A title such as 'Filter by range [7 days]' is a
# person's words and stays whole.
_TITLE_TAIL_RE = re.compile(
    r'(?:\[(?:chromium|firefox|webkit|chrome|msedge)[^\]]*\](?:@[\w -]+)?|\[[^\]]*\]@[\w -]+)$')


def clean_title(title: str) -> str:
    """A title as an older archive stored it, without a '[chromium]@account users' tail."""
    cleaned = _TITLE_TAIL_RE.sub('', title).strip()
    return cleaned or title


def module_of(nodeid: str) -> str:
    """The test file's path under tests/ - the grouping key of the page and
    the health summary. A path outside tests/ is kept whole."""
    path = nodeid.split('::', 1)[0].replace('\\', '/')
    return path.removeprefix('tests/')


def duration_text(value: object) -> str:
    """format_seconds for a summary value, '-' when it is missing or not
    a number: the page renders, never raises."""
    try:
        return format_seconds(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return '-'


def format_seconds(seconds: float) -> str:
    """12.3 s under a minute, else 1 min 41 s."""
    if seconds < 60:
        return f'{seconds:.1f} s'
    minutes, rest = divmod(round(seconds), 60)
    return f'{int(minutes)} min {rest} s'


def parse_started(text: str | None) -> datetime | None:
    """A summary 'started' value or a run folder's stamp as a datetime, or
    None when the text has neither shape."""
    m = _STARTED_RE.match(str(text or '').strip())
    if not m:
        return None
    year, month, day, hour, minute, second = (int(g or 0) for g in m.groups())
    try:
        return datetime(year, month, day, hour, minute, second)  # noqa: DTZ001 - local-time display of a run timestamp; tz would shift the shown time
    except ValueError:
        return None


def friendly_time(text: str | None) -> str:
    """Aug 24, 2:27 PM - the form a reader recognises. Month names are
    fixed here, not taken from the locale, so two machines agree. The
    raw text stands in when it does not parse."""
    dt = parse_started(text)
    if dt is None:
        return str(text or '-')
    hour = dt.hour % 12 or 12
    return f'{_MONTHS[dt.month - 1]} {dt.day}, {hour}:{dt.minute:02d} {"AM" if dt.hour < 12 else "PM"}'


def friendly_stamp(text: str | None) -> str:
    """Aug 25, 2026, 10:17 AM - friendly_time with the year, for a list
    that spans months (the archive index) and a page title that stands
    alone. The raw text stands in when it does not parse."""
    dt = parse_started(text)
    if dt is None:
        return str(text or '-')
    return f'{friendly_date(text, with_year=True)}, {friendly_time(text).split(", ", 1)[1]}'


def friendly_date(text: str | None, with_year: bool = False) -> str:
    """Aug 24, or Aug 24, 2026 with the year: the day alone, for a span of
    runs ("Aug 21 to Aug 24, 2026"). The raw text stands in when it does
    not parse."""
    dt = parse_started(text)
    if dt is None:
        return str(text or '-')
    day = f'{_MONTHS[dt.month - 1]} {dt.day}'
    return f'{day}, {dt.year}' if with_year else day


def run_started(run_dir: Path, summary: Mapping | None = None) -> str:
    """When the run started: summary.json's value, else the folder stamp."""
    started = (summary or {}).get('started')
    return str(started) if started else run_dir.name.partition('__')[0]


def first_line(text: str) -> str:
    return next((line.strip() for line in (text or '').splitlines() if line.strip()), '')


# xdist prefixes a worker's longrepr with its banner, '[gw1] win32 -- Python 3.14.6 C:\...':
# true of every test on that worker, so never the line that names a failure.
_WORKER_BANNER_RE = re.compile(r'^\[gw\d+\] \S+ -- Python \S+.*$', re.MULTILINE)


def error_line(longrepr: str) -> str:
    """The one line that names a failure: the first 'E ' line of pytest's
    longrepr, else its first non-empty line, capped for summary.json. An
    xdist worker banner is skipped: a crashed worker's stand-in report
    carries the banner first and the crash message after it."""
    text = _WORKER_BANNER_RE.sub('', longrepr or '')
    return first_line(_headline(text) or text)[:ERROR_LINE_MAX]


def error_signature(text: str) -> str:
    """The error's first line with the parts that vary between runs reduced.

    Two failures share a signature when only their numbers (timeouts,
    counts, ids), quoted values, hex addresses, or locator text differ:
    a 30000 ms and a 15000 ms timeout on the same step are one error, and
    so are "group 'AutoTest 131103' not found" on two days. Empty for an
    empty error, so a failure without a message never groups.
    """
    line = first_line(text)
    # A raw longrepr line still carries pytest's 'E ' prefix.
    prefixed = _ERROR_LINE_RE.match(line)
    if prefixed:
        line = prefixed.group(1).strip()
    line = _SIG_OBJECT_RE.sub('<object>', line)
    line = _SIG_LOCATOR_RE.sub(r'\1(...)', line)
    line = _SIG_QUOTED_RE.sub('"..."', line)
    line = _SIG_HEX_RE.sub('0x...', line)
    line = _SIG_NUMBER_RE.sub('#', line)
    return re.sub(r'\s+', ' ', line).strip()


def _loose(text: str) -> str:
    """Case- and punctuation-insensitive form for the last-resort match.

    pytest-playwright names its per-test folders with python-slugify
    (lowercase, underscores to hyphens), so an exact comparison with the
    repo's sanitizer never fits; this form makes both spellings equal.
    """
    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')


def _digest(stem: str) -> str:
    """The 8-char digest run_archive._fit_dest puts into a shortened name."""
    return hashlib.sha256(stem.encode('utf-8')).hexdigest()[:8]


def parse_screenshot_name(stem: str) -> dict | None:
    """Split a screenshot stem into its test part, suffix, and frame index.

    <safe>__PASSED, <safe>__FAILED__mid-drag-2, <safe>__SKIPPED__mid-drag-1.
    None when the stem carries no label - not one of report_media's files.
    """
    m = _SCREENSHOT_RE.match(stem)
    if not m:
        return None
    return {'test': m.group('test'), 'suffix': m.group('suffix'),
            'label': m.group('label'), 'index': m.group('index')}


def parse_artifact_name(relpath: str) -> dict | None:
    """Split a path under traces/ into its test part and kind.

    Flat conftest files: <safe>-trace.zip, <safe>-video.webm,
    <safe>-login-failure-trace.zip. pytest-playwright's per-test folders
    hold trace.zip / video.webm / *.png under a folder named for the test,
    so there the folder is the test part.
    """
    path = Path(relpath)
    ext = path.suffix.lower()
    if ext not in ('.zip', '.webm', '.png'):
        return None
    if len(path.parts) > 1:
        kind = {'.zip': 'trace', '.webm': 'video', '.png': 'screenshot'}[ext]
        return {'test': path.parts[0], 'kind': kind, 'nested': True}
    m = _ARTIFACT_RE.match(path.stem)
    if not m:
        return None
    return {'test': m.group('test'), 'kind': m.group('kind'), 'nested': False}


def match_test(test_part: str, safes: dict[str, str],
               full_stem: Callable[[str], str] | None = None) -> str | None:
    """Find which test an archived file's test part names.

    `safes` maps each known test's sanitized stem to its node id.
    `full_stem` rebuilds the file's original stem from a sanitized stem
    (for a final screenshot: safe + '__FAILED'), which is what the digest
    of a shortened name was computed from.

    Tiers, first hit wins:
      1. Exact stem.
      2. Shortened name whose digest recomputes from a known test's full
         stem - the only tier that tells parametrized twins apart.
      3. Shortened name whose head and tail both sit inside exactly one
         known stem (a digest from an older layout).
      4. Longest common prefix that covers the whole test part (or its
         head), then the same on the loose forms for slugified names.
    None when nothing matches: the file is listed as unattached.
    """
    if test_part in safes:
        return safes[test_part]
    splits = [(test_part[:m.start()], m.group(1), test_part[m.end():])
              for m in _DIGEST_RE.finditer(test_part)]
    for head, digest, tail in splits:
        for safe, nodeid in safes.items():
            if not (safe.startswith(head) and safe.endswith(tail)):
                continue
            if full_stem is not None and _digest(full_stem(safe)) == digest:
                return nodeid
    for head, _, tail in splits:
        inside = [nodeid for safe, nodeid in safes.items()
                  if safe.startswith(head) and safe.endswith(tail)]
        if len(inside) == 1:
            return inside[0]
    heads = [test_part, *[head for head, _d, _t in splits]]
    best: tuple[int, str] | None = None
    for head in heads:
        for safe, nodeid in safes.items():
            n = len(os.path.commonprefix([head, safe]))
            if n and n >= min(len(head), len(safe)) and (best is None or n > best[0]):
                best = (n, nodeid)
    if best is not None:
        return best[1]
    loose_part = _loose(test_part)
    for safe, nodeid in safes.items():
        loose_safe = _loose(safe)
        related = loose_safe.startswith(loose_part) or loose_part.startswith(loose_safe)
        if loose_part and loose_safe and related and (best is None or len(loose_safe) > best[0]):
            best = (len(loose_safe), nodeid)
    return best[1] if best else None


# ---------------------------------------------------------------- sources

def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def load_summary(run_dir: Path) -> dict:
    """summary.json as a dict; empty when missing or unreadable."""
    loaded = _read_json(run_dir / 'summary.json')
    return loaded if isinstance(loaded, dict) else {}


def summary_tests(summary: Mapping) -> list[dict]:
    """The per-test records of a summary.json, keyed rows only.

    Archives written before the "tests" key existed have none; the page
    then falls back to report.html and the file names as before.
    """
    rows = summary.get('tests')
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict) and r.get('nodeid')]


def find_earlier_runs(run_dir: Path, limit: int) -> list[Path]:
    """The newest `limit` earlier archives of the same target, newest first.

    Same target means the same folder slug: the part after '__'. Only
    folders that sort before this run count, so rebuilding an old run's
    page compares it with its own past, not with a later run.
    """
    slug = run_dir.name.partition('__')[2]
    parent = run_dir.parent
    if not slug or limit < 1 or not parent.is_dir():
        return []
    earlier = sorted((p for p in parent.iterdir()
                      if p.is_dir() and RUN_DIR_RE.match(p.name)
                      and p.name < run_dir.name and p.name.partition('__')[2] == slug),
                     reverse=True)
    return earlier[:limit]


def find_previous_run(run_dir: Path) -> Path | None:
    """The newest earlier archive of the same target, or None."""
    earlier = find_earlier_runs(run_dir, 1)
    return earlier[0] if earlier else None


class ByTest(dict):
    """A dict keyed by test, where a test is its node id without xdist's '@<group>' suffix.

    A test that got an xdist_group mark reports as 'test_x[chromium]@account_views' from
    then on, and as 'test_x[chromium]' in a run with no -n. Keyed on the raw text, every
    grouped test read as a new test with no history (found 09/21/2026). Facts of an
    earlier run go in through here, and each lookup by a node id of this run finds them.
    """

    def __init__(self, pairs: Mapping[str, str] | None = None) -> None:
        super().__init__((canonical_nodeid(str(k)), v) for k, v in dict(pairs or {}).items())

    def __contains__(self, key: object) -> bool:
        return super().__contains__(canonical_nodeid(str(key)))

    def __getitem__(self, key: str) -> str:
        return super().__getitem__(canonical_nodeid(str(key)))

    def get(self, key: str, default=None):
        return super().get(canonical_nodeid(str(key)), default)


def load_previous_outcomes(prev_dir: Path) -> dict[str, str]:
    """Node id -> outcome for an earlier run, from its summary "tests" when
    present, else from its report and file names (older archives)."""
    rows = summary_tests(load_summary(prev_dir))
    if rows:
        return ByTest({str(r['nodeid']): str(r.get('outcome') or 'unknown') for r in rows})
    records, _unattached = collect_tests(prev_dir)
    return ByTest({nodeid: rec.outcome for nodeid, rec in records.items()})


def compute_deltas(current: Mapping[str, str],
                   previous: Mapping[str, str] | None) -> dict[str, str]:
    """The change chip of each test since the previous run of this target.

    No previous run means no chips at all - nothing to compare with.
    A test absent from the previous run is 'new test'. Failing both
    times is 'still failing'; failing now after a pass or a skip is
    'new failure'; passing now after a failure is 'fixed'. Every other
    pair (pass then pass, skip either side) gets no chip.
    """
    if previous is None:
        return {}
    chips: dict[str, str] = {}
    for nodeid, outcome in current.items():
        failing = outcome in FAILURE_OUTCOMES
        if nodeid not in previous:
            chips[nodeid] = DELTA_NEW_TEST
            continue
        was_failing = previous[nodeid] in FAILURE_OUTCOMES
        if failing and was_failing:
            chips[nodeid] = DELTA_STILL_FAILING
        elif failing:
            chips[nodeid] = DELTA_NEW_FAILURE
        elif was_failing and outcome in PASSED_OUTCOMES:
            chips[nodeid] = DELTA_FIXED
    return chips


def load_run_errors(run_dir: Path) -> dict[str, str]:
    """Node id -> the error line of each failed test of an archived run.

    From the summary records when the archive wrote them (schema 3), else
    from the captured logs in report.html (older archives). Empty when
    neither knows, or when nothing failed.
    """
    rows = summary_tests(load_summary(run_dir))
    failed = [r for r in rows if str(r.get('outcome') or '') in FAILURE_OUTCOMES]
    if rows and all('error' in r for r in failed):
        return ByTest({str(r['nodeid']): str(r.get('error') or '') for r in failed})
    return {nodeid: first_line(_headline(row['log']))
            for nodeid, row in load_report_results(run_dir).items()
            if row['outcome'] in FAILURE_OUTCOMES}


def load_history(run_dir: Path, limit: int = HISTORY_RUNS) -> list[dict]:
    """The earlier runs of this target the page compares with, newest
    first: {'name', 'started', 'utc_offset', 'outcomes', 'errors'} each. At most
    limit - 1 of them, so that with this run the window holds `limit`.
    'utc_offset' is the earlier run's own: a page built on a laptop showed the
    times of earlier box runs in the laptop's zone, 5 hours off (audit 09/21/2026)."""
    history = []
    for earlier in find_earlier_runs(run_dir, limit - 1):
        summary = load_summary(earlier)
        history.append({'name': earlier.name, 'started': run_started(earlier, summary),
                        'utc_offset': summary.get('utc_offset'),
                        # so the page can say the camera changed between 2 runs
                        'camera': (summary.get('provenance') if isinstance(summary.get('provenance'), dict) else {}).get('camera'),
                        'outcomes': load_previous_outcomes(earlier),
                        'errors': load_run_errors(earlier)})
    return history


def test_results(nodeid: str, runs: list[dict]) -> list[dict]:
    """The test's outcome in each run that ran it, in the runs' order."""
    return [{'run': r['name'], 'started': r.get('started'), 'utc_offset': r.get('utc_offset'),
             'outcome': r['outcomes'][nodeid]}
            for r in runs if nodeid in (r.get('outcomes') or {})]


def history_facts(results: list[dict], limit: int = HISTORY_RUNS) -> dict:
    """The history strip's facts from a test's results, newest first.

    'blocks' are the newest `limit` results, oldest first (the strip reads
    left to right). 'rate' is the flaky rate: failures / runs the test was
    present in, None with no runs. 'flaky' is true when the window holds
    at least one pass and one failure. 'last_failure' is the newest
    failing block, or None.
    """
    blocks = list(reversed(results[:limit]))
    passed = sum(1 for b in blocks if b['outcome'] in PASSED_OUTCOMES)
    failures = [b for b in blocks if b['outcome'] in FAILURE_OUTCOMES]
    runs = len(blocks)
    return {'blocks': blocks, 'runs': runs, 'passed': passed, 'failed': len(failures),
            'rate': (len(failures) / runs) if runs else None,
            'flaky': passed > 0 and len(failures) > 0,
            'last_failure': failures[-1] if failures else None}


def signatures_seen(history: list[dict]) -> dict[str, dict]:
    """Error signature -> the newest earlier run that showed it, as
    {'run', 'started', 'utc_offset'}. `history` is newest first, as load_history gives it."""
    seen: dict[str, dict] = {}
    for run in history:
        for line in (run.get('errors') or {}).values():
            signature = error_signature(line)
            if signature and signature not in seen:
                seen[signature] = {'run': run['name'], 'started': run.get('started'),
                                   'utc_offset': run.get('utc_offset')}
    return seen


ONE_CAUSE_MIN = 5        # failures sharing one error signature before it is called the cause
ONE_SKIP_MIN = 20        # skips sharing one reason before the run says so in one line


def one_cause(tests: Iterable[Mapping[str, Any]]) -> dict | None:
    """The error most of the failures share, when at least ONE_CAUSE_MIN do.

    {'count', 'of', 'line', 'first'}: how many failures carry the
    signature, of how many failures, the first one's error line, and its
    node id. None when no signature reaches the bar.
    """
    failing = [t for t in tests if str(t.get('outcome')) in FAILURE_OUTCOMES]
    by_sig: dict[str, list[Mapping[str, Any]]] = {}
    for t in failing:
        sig = error_signature(str(t.get('error') or ''))
        if sig:
            by_sig.setdefault(sig, []).append(t)
    if not by_sig:
        return None
    group = max(by_sig.values(), key=len)
    if len(group) < ONE_CAUSE_MIN:
        return None
    return {'count': len(group), 'of': len(failing), 'line': str(group[0].get('error') or ''),
            'first': str(group[0].get('nodeid') or '')}


def one_skip_reason(tests: Iterable[Mapping[str, Any]]) -> dict | None:
    """The reason most of the skips share, when more than ONE_SKIP_MIN do: {'count', 'reason'}."""
    by_reason: dict[str, list[str]] = {}
    for t in tests:
        if str(t.get('outcome')) == 'skipped' and t.get('reason'):
            by_reason.setdefault(error_signature(str(t['reason'])), []).append(str(t['reason']))
    if not by_reason:
        return None
    group = max(by_reason.values(), key=len)
    if len(group) <= ONE_SKIP_MIN:
        return None
    return {'count': len(group), 'reason': group[0]}


def group_failures(failures: list[TestRecord]) -> list[list[TestRecord]]:
    """Failures grouped by signature, each group placed where its first
    member was. A failure without a signature stands alone."""
    groups: dict[str, list[TestRecord]] = {}
    ordered: list[list[TestRecord]] = []
    for rec in failures:
        if not rec.signature:
            ordered.append([rec])
            continue
        if rec.signature not in groups:
            groups[rec.signature] = []
            ordered.append(groups[rec.signature])
        groups[rec.signature].append(rec)
    return ordered


def plural(count: int, one: str, many: str) -> str:
    return f'{count} {one if count == 1 else many}'


def run_verdict(passed: int, failed: int, skipped: int,
                deltas: Mapping[str, str] | None = None, *,
                not_run: int = 0, reruns: int = 0) -> str:
    """The run in one sentence of plain words, numbers exact.

    "3 of 49 tests failed - 2 new failures, 1 still failing; 1 test
    skipped." or "All 49 tests passed." `failed` counts errors too: a
    test that could not start did not pass. The change counts come from
    the deltas against the previous run and are left out without one.
    """
    total = passed + failed + skipped
    if total == 0:
        return 'No tests ran.'
    tests = 'test' if total == 1 else 'tests'
    if failed:
        text = f'{failed} of {total} {tests} failed'
    elif skipped:
        text = f'{passed} of {total} {tests} passed'
    elif total == 1:
        text = '1 test ran and passed'
    else:
        text = f'All {total} tests passed'
    labels = list((deltas or {}).values())
    changes = [(labels.count(DELTA_NEW_FAILURE), 'new failure', 'new failures'),
               (labels.count(DELTA_STILL_FAILING), 'still failing', 'still failing'),
               (labels.count(DELTA_FIXED), 'fixed', 'fixed')]
    detail = ', '.join(plural(n, one, many) for n, one, many in changes if n)
    if detail:
        text += f' - {detail}'
    if skipped:
        text += f'; {plural(skipped, "test skipped", "tests skipped")}'
    if reruns:
        text += f'; {plural(reruns, "test passed on a rerun", "tests passed on a rerun")}'
    if not_run:
        text += f'; {plural(not_run, "collected test did not run", "collected tests did not run")}'
    return text + '.'


def delta_wording(previous: Mapping | None,
                  link: Callable[[str], str] | None = None) -> str:
    """The sentence that says what the change labels compare against.

    With a previous run: "Changes since the previous run of these tests
    (Aug 24, 2:27 PM)", where `link` wraps the run's name and time when
    the page wants a link. Without one: "First run of these tests -
    nothing to compare yet."
    """
    if previous is None:
        return 'First run of these tests - nothing to compare yet.'
    label = f'previous run of these tests ({friendly_time(previous.get("started"))})'
    return f'Changes since the {link(label) if link else label}'


def load_report_results(run_dir: Path) -> dict[str, dict]:
    """Per-test outcome, duration, and log from pytest-html's JSON blob.

    pytest-html 4 writes the results table's data into a data-jsonblob
    attribute (HTML-escaped JSON; the log inside is escaped once more).
    A test id maps to a list of rows; the worst row's outcome is the
    test's, and its log is kept.
    """
    try:
        text = (run_dir / 'report.html').read_text(encoding='utf-8', errors='replace')
    except OSError:
        return {}
    m = re.search(r'data-jsonblob="([^"]*)"', text)
    if not m:
        return {}
    try:
        blob = json.loads(html.unescape(m.group(1)))
    except ValueError:
        return {}
    tests = blob.get('tests') if isinstance(blob, dict) else None
    if not isinstance(tests, dict):
        return {}
    out: dict[str, dict] = {}
    for nodeid, rows in tests.items():
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            outcome = _HTML_OUTCOMES.get(str(row.get('result', '')).lower())
            if outcome is None:
                continue  # Rerun rows are not a final verdict
            current = out.get(nodeid)
            if current is None or _OUTCOME_RANK[outcome] >= _OUTCOME_RANK[current['outcome']]:
                out[nodeid] = {'outcome': outcome,
                               'duration': str(row.get('duration', '') or ''),
                               'log': html.unescape(str(row.get('log', '') or ''))}
    return out


def load_results_json(run_dir: Path) -> dict[str, str]:
    """Per-nodeid outcomes from qatouch_reporter's last_run_results.json.

    Accepts the reporter's {'results': [{'nodeid', 'outcome'}, ...]} shape
    and a plain {nodeid: outcome} map.
    """
    loaded = _read_json(run_dir / 'last_run_results.json')
    if not isinstance(loaded, dict):
        return {}
    rows = loaded.get('results')
    pairs = []
    if isinstance(rows, list):
        pairs = [(r.get('nodeid'), r.get('outcome')) for r in rows if isinstance(r, dict)]
    else:
        pairs = [(k, v) for k, v in loaded.items() if isinstance(v, str)]
    out = {}
    for nodeid, outcome in pairs:
        if not nodeid:
            continue
        outcome = str(outcome or '').lower()
        out[str(nodeid)] = _HTML_OUTCOMES.get(outcome, 'unknown')
    return out


# ---------------------------------------------------------------- trace digest

def _error_message(node) -> str:
    """Dig the human message out of a trace error object, whatever its nesting."""
    if isinstance(node, dict):
        if isinstance(node.get('message'), str):
            return node['message']
        for key in ('error', 'value'):
            if key in node:
                found = _error_message(node[key])
                if found:
                    return found
    if isinstance(node, str):
        return node
    return ''


def _api_name(ev: dict) -> str:
    name = ev.get('apiName')
    if name:
        return str(name)
    cls, method = ev.get('class'), ev.get('method')
    return f'{cls}.{method}' if cls or method else str(ev.get('type', '?'))


# A selector that names a secret field: input[name="password"], [type=password], #pwd, "Passcode".
_SECRET_FIELD_RE = re.compile(r'pass|pwd|secret|token|otp', re.IGNORECASE)
_TYPED_KEYS = ('text', 'value')


def _params_text(ev: dict) -> str:
    """The parameters of a traced action, as one line for the actions list.

    What a test typed into a secret field is masked. A trace holds the login's fill call, and
    the page printed it: `fill selector=input[name="password"] value=<the account password>`.
    The evidence scan masked it before it was served (found 09/21/2026). It is not printed now.
    """
    params = ev.get('params') or {}
    secret_field = bool(_SECRET_FIELD_RE.search(str(params.get('selector') or '')))
    bits = []
    for key in ('selector', 'url', 'text', 'value', 'key', 'expression', 'state'):
        if key in params:
            val = str(params[key]).replace('\n', ' ')
            if secret_field and key in _TYPED_KEYS:
                val = '***'
            bits.append(f'{key}={val[:120]}')
    return ' '.join(bits)


def digest_trace(zip_path: Path, last_actions: int = LAST_ACTIONS) -> dict:
    """Pull actions, errors, console errors, and the last URL from a trace.zip.

    Reads the JSON-lines *.trace files inside the zip: 'before'/'after'
    pairs are the actions (an 'after' with an error is the failure),
    'log' events are an action's call log, 'console' events carry the
    page's console, and the newest 'frame-snapshot' names the final URL.
    Returns {'actions': [{'name', 'params', 'error', 'log'}], 'errors',
    'console', 'url', 'files'}; the actions window ends at the last
    failing action, or holds the last `last_actions` when none failed.
    """
    out: dict = {'actions': [], 'errors': [], 'console': [], 'url': None, 'files': 0}
    try:
        zf = zipfile.ZipFile(zip_path)
    except (OSError, zipfile.BadZipFile) as e:
        out['errors'].append(f'could not open trace: {e}')
        return out
    with zf:
        names = zf.namelist()
        out['files'] = len(names)
        calls: dict[str, dict] = {}
        order: list[str] = []
        logs: dict[str, list[str]] = {}
        for name in (n for n in names if n.endswith('.trace')):
            for raw in zf.read(name).decode('utf-8', errors='replace').splitlines():
                try:
                    ev = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(ev, dict):
                    continue
                kind = ev.get('type')
                call_id = str(ev.get('callId', ''))
                if kind == 'before':
                    calls[call_id] = {'name': _api_name(ev), 'params': _params_text(ev),
                                      'error': '', 'log': []}
                    order.append(call_id)
                elif kind == 'after':
                    msg = _error_message(ev.get('error'))
                    if call_id in calls and msg:
                        calls[call_id]['error'] = msg
                elif kind == 'log' and call_id:
                    logs.setdefault(call_id, []).append(str(ev.get('message', ''))[:200])
                elif kind == 'console':
                    if str(ev.get('messageType', '')).lower() == 'error':
                        out['console'].append(str(ev.get('text', ''))[:200])
                elif kind == 'event' and ev.get('method') == 'console':
                    params = ev.get('params') or {}
                    if str(params.get('type', '')).lower() == 'error':
                        out['console'].append(str(params.get('text', ''))[:200])
                elif kind == 'frame-snapshot':
                    snap = ev.get('snapshot') or {}
                    if snap.get('frameUrl'):
                        out['url'] = snap['frameUrl']
    failing = [cid for cid in order if calls[cid]['error']]
    if failing:
        idx = order.index(failing[-1])
        window = order[max(0, idx - last_actions):idx + 1]
    else:
        window = order[-last_actions:]
    for cid in window:
        call = dict(calls[cid])
        call['log'] = logs.get(cid, [])[-8:] if call['error'] else []
        out['actions'].append(call)
    out['errors'] = [calls[cid]['error'].splitlines()[0][:200] for cid in failing]
    return out


# ---------------------------------------------------------------- collect

def _worse(new: str, old: str) -> bool:
    return _OUTCOME_RANK.get(new, -1) > _OUTCOME_RANK.get(old, -1)


def _list_files(root: Path, patterns: tuple[str, ...]) -> list[Path]:
    if not root.is_dir():
        return []
    found = [p for pattern in patterns for p in root.rglob(pattern) if p.is_file()]
    return sorted(set(found))


def collect_tests(run_dir: Path) -> tuple[dict[str, TestRecord], list[str]]:
    """Merge every source in the run folder into one record per test.

    Returns the records keyed by node id plus the relative paths of files
    that matched no test (shown at the end of the page rather than lost).
    """
    tests: dict[str, TestRecord] = {}
    summary = load_summary(run_dir)

    for row in summary_tests(summary):
        nodeid = str(row['nodeid'])
        rec = tests.setdefault(nodeid, TestRecord(nodeid))
        rec.title = clean_title(str(row.get('title') or ''))
        rec.error = str(row.get('error') or '')
        rec.module = str(row.get('module') or '')
        rec.case_key = str(row.get('case_key') or '')
        rec.case_title = str(row.get('case_title') or '')
        rec.product = str(row.get('product') or '')
        rec.log_file = str(row.get('log_file') or '')
        if isinstance(row.get('duration_s'), (int, float)):
            rec.seconds = float(row['duration_s'])
        outcome = _HTML_OUTCOMES.get(str(row.get('outcome') or '').lower())
        if outcome and _worse(outcome, rec.outcome):
            rec.outcome = outcome
        rec.recorded = bool(outcome)
        rec.reruns = int(row.get('reruns') or 0)
        rec.reason = str(row.get('reason') or '')
        rec.phases = dict(row.get('phases') or {}) if isinstance(row.get('phases'), dict) else {}
        rec.known = dict(row['known_defect']) if isinstance(row.get('known_defect'), dict) else None
    for nodeid, row in load_report_results(run_dir).items():
        rec = tests.setdefault(nodeid, TestRecord(nodeid))
        rec.duration, rec.log = row['duration'], row['log']
        if _worse(row['outcome'], rec.outcome):
            rec.outcome = row['outcome']
    for nodeid, outcome in load_results_json(run_dir).items():
        rec = tests.setdefault(nodeid, TestRecord(nodeid))
        if _worse(outcome, rec.outcome):
            rec.outcome = outcome
    for item in summary.get('advisories') or []:
        if not isinstance(item, dict):
            continue
        nodeid, line = str(item.get('test', '')), str(item.get('line', ''))
        if nodeid and line:
            tests.setdefault(nodeid, TestRecord(nodeid)).advisories.append(line)

    unattached: list[str] = []

    def rel(path: Path) -> str:
        return path.relative_to(run_dir).as_posix()

    def owner(test_part: str, cap: int, full_stem: Callable[[str], str] | None,
              fallback_outcome: str) -> TestRecord:
        safes = {_safe_name(t.nodeid, cap): t.nodeid for t in tests.values()}
        nodeid = match_test(test_part, safes, full_stem)
        if nodeid is None:
            # No listed test owns the file: the name is the only witness,
            # so it becomes the test's stand-in record.
            nodeid = test_part
            rec = tests.setdefault(nodeid, TestRecord(nodeid))
            if _worse(fallback_outcome, rec.outcome):
                rec.outcome = fallback_outcome
        return tests[nodeid]

    for png in _list_files(run_dir / 'screenshots', ('*.png',)):
        parsed = parse_screenshot_name(png.stem)
        if parsed is None:
            unattached.append(rel(png))
            continue
        suffix = parsed['suffix']
        frame = f"__{parsed['label']}-{parsed['index']}" if parsed['label'] else ''
        rec = owner(parsed['test'], SAFE_NAME_MAX,
                    lambda safe, s=suffix, f=frame: f'{safe}__{s}{f}',
                    _SUFFIX_OUTCOMES[suffix])
        (rec.frames if frame else rec.screenshots).append(rel(png))
        if not rec.recorded and _worse(_SUFFIX_OUTCOMES[suffix], rec.outcome):
            rec.outcome = _SUFFIX_OUTCOMES[suffix]

    traces_dir = run_dir / 'traces'
    for artifact in _list_files(traces_dir, ('*.zip', '*.webm', '*.png')):
        parsed = parse_artifact_name(artifact.relative_to(traces_dir).as_posix())
        if parsed is None:
            unattached.append(rel(artifact))
            continue
        kind = parsed['kind']
        full_stem = None if parsed['nested'] else (lambda safe, k=kind: f'{safe}-{k}')
        # A kept trace or video means the test failed; a login-failure
        # trace means its setup did.
        fallback = 'error' if kind == 'login-failure-trace' else 'failed'
        rec = owner(parsed['test'], TRACE_NAME_MAX, full_stem, fallback)
        if kind == 'video':
            rec.videos.append(rel(artifact))
        elif kind == 'screenshot':
            rec.screenshots.append(rel(artifact))
        else:
            rec.traces.append(rel(artifact))
        if kind != 'screenshot' and not rec.recorded and _worse(fallback, rec.outcome):
            rec.outcome = fallback

    return tests, unattached


# ---------------------------------------------------------------- render

PAGE_CSS = '''
:root { color-scheme: light dark;
  --bg: #fafafa; --fg: #1b1b1b; --muted: #5f6368; --card: #ffffff; --line: #d9d9d9;
  --fail: #b3261e; --fail-bg: #fdecea; --pass: #1e7d34; --pass-bg: #e8f5e9;
  --skip: #8a6d00; --skip-bg: #fff6d6; --code-bg: #f1f3f4; --link: #0b57d0; }
@media (prefers-color-scheme: dark) { :root {
  --bg: #121212; --fg: #e6e6e6; --muted: #a0a0a0; --card: #1e1e1e; --line: #3a3a3a;
  --fail: #ff8a80; --fail-bg: #3a1f1d; --pass: #81c995; --pass-bg: #1d2f22;
  --skip: #ffd54f; --skip-bg: #3a331a; --code-bg: #262626; --link: #8ab4f8; } }
* { box-sizing: border-box; }
body { margin: 0; padding: 1.5rem; background: var(--bg); color: var(--fg);
  font: 15px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
a { color: var(--link); }
h1 { font-size: 1.5rem; margin: 0 0 .5rem; }
h2 { font-size: 1.2rem; margin: 2rem 0 .75rem; border-bottom: 1px solid var(--line); padding-bottom: .25rem; }
h3 { font-size: 1rem; margin: 0 0 .5rem; word-break: break-all; }
h4 { font-size: .9rem; margin: 1rem 0 .35rem; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }
.meta { color: var(--muted); display: flex; flex-wrap: wrap; gap: .4rem 1.25rem; margin-bottom: .75rem; }
.chips { display: flex; flex-wrap: wrap; gap: .5rem; margin: .5rem 0 1rem; }
.chip { border-radius: 999px; padding: .15rem .7rem; font-weight: 600; border: 1px solid var(--line); background: var(--card); }
.chip.failed, .chip.error, .chip.errors { color: var(--fail); background: var(--fail-bg); border-color: var(--fail); }
.chip.passed { color: var(--pass); background: var(--pass-bg); border-color: var(--pass); }
.chip.skipped { color: var(--skip); background: var(--skip-bg); border-color: var(--skip); }
.card { background: var(--card); border: 1px solid var(--line); border-left: 5px solid var(--fail);
  border-radius: 6px; padding: 1rem 1.25rem; margin-bottom: 1.25rem; }
.card.error { border-left-color: var(--skip); }
pre { background: var(--code-bg); padding: .75rem; border-radius: 4px; overflow: auto;
  font: 13px/1.4 ui-monospace, Consolas, "Courier New", monospace; white-space: pre-wrap; word-break: break-word; margin: 0; }
pre.headline { color: var(--fail); font-weight: 600; }
.actions { list-style: none; margin: 0; padding: 0; font: 13px/1.5 ui-monospace, Consolas, monospace; }
.actions li { padding: .1rem 0 .1rem .5rem; border-left: 3px solid transparent; }
.actions li.failing { border-left-color: var(--fail); background: var(--fail-bg); }
.actions .err { color: var(--fail); display: block; }
.actions .log { color: var(--muted); display: block; padding-left: 1.5rem; }
.shots { display: flex; flex-wrap: wrap; gap: .75rem; }
.shots figure { margin: 0; max-width: 320px; }
.shots img { width: 100%; height: auto; border: 1px solid var(--line); border-radius: 4px; display: block; }
.shots figcaption { font-size: .8rem; color: var(--muted); word-break: break-all; }
video { max-width: 100%; width: 640px; border: 1px solid var(--line); border-radius: 4px; background: #000; }
details { margin: .5rem 0; }
summary { cursor: pointer; font-weight: 600; }
details.section > summary { font-size: 1.05rem; }
.row { display: flex; flex-wrap: wrap; align-items: flex-start; gap: 1rem; padding: .6rem 0; border-bottom: 1px solid var(--line); }
.row-main { flex: 1 1 auto; min-width: 0; }
.row .shots { flex: 0 0 auto; }
.row .shots figure { max-width: 160px; }
.row .facts { margin: .2rem 0 0; }
.sub { display: flex; flex-direction: column; gap: .15rem; margin-top: .15rem; }
.sub .nodeid { word-break: normal; overflow-wrap: anywhere; }
.sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
.dur { color: var(--muted); font-size: .85rem; margin-left: .5rem; }
ul.plain { margin: .25rem 0; padding-left: 1.25rem; }
.empty { color: var(--muted); font-style: italic; }
.title { font-weight: 600; word-break: break-word; }
h3.title { margin: 0; }
.case { color: var(--muted); font-size: .9rem; }
.nodeid { color: var(--muted); font-size: .78rem; font-family: ui-monospace, Consolas, monospace; word-break: break-all; }
.facts { display: flex; flex-wrap: wrap; align-items: center; gap: .4rem .9rem; margin: .35rem 0; font-size: .85rem; color: var(--muted); }
.chip.delta { font-size: .78rem; padding: .05rem .55rem; }
.chip.new-failure, .chip.still-failing { color: var(--fail); background: var(--fail-bg); border-color: var(--fail); }
.chip.fixed { color: var(--pass); background: var(--pass-bg); border-color: var(--pass); }
.chip.new-test { color: var(--link); border-color: var(--link); }
.chip.new-error { color: var(--fail); border-color: var(--fail); }
.chip.seen-before { color: var(--muted); }
.chip.flaky { color: var(--skip); background: var(--skip-bg); border-color: var(--skip); }
.verdict { font-size: 1.1rem; font-weight: 600; margin: .25rem 0 .75rem; }
ul.read-first { border-left: 4px solid var(--fail); margin: 0 0 1rem; padding: .4rem .4rem .4rem 1.6rem; background: var(--fail-bg); }
/* The run header is a ruled ledger: 1 heavy rule above, hairlines between the facts, no boxes and no
   side accents (Justin, 09/21/2026). The band clips the hairline of the first fact of each row. */
.ledger { overflow: hidden; border-top: 2px solid var(--fg); border-bottom: 1px solid var(--line); margin: .6rem 0 1rem; }
dl.run-facts { display: flex; flex-wrap: wrap; row-gap: .7rem; margin: .6rem 0 .6rem calc(-1.1rem - 1px); padding: 0; }
dl.run-facts .fact { padding: 0 1.1rem; border-left: 1px solid var(--line); }
dl.run-facts dt { font-size: .66rem; text-transform: uppercase; letter-spacing: .09em; color: var(--muted); margin: 0; }
dl.run-facts dd { margin: .1rem 0 0; font-weight: 600; color: var(--fg); word-break: break-word;
  font-variant-numeric: tabular-nums; }
dl.run-facts .fact.lead dd { font-weight: 700; }
dl.run-facts .fact.pass dd, dl.run-facts .fact.fail dd { display: flex; align-items: center; gap: .4rem; }
dl.run-facts .fact.pass dd::before, dl.run-facts .fact.fail dd::before { content: ""; width: .55rem; height: .55rem; border-radius: 50%; }
dl.run-facts .fact.pass dd { color: var(--pass); }
dl.run-facts .fact.pass dd::before { background: var(--pass); }
dl.run-facts .fact.fail dd { color: var(--fail); }
dl.run-facts .fact.fail dd::before { background: var(--fail); }
dl.run-facts dd .note { font-weight: 400; color: var(--skip); }
details.card { padding: 0; }
details.card > summary { list-style-position: outside; padding: .8rem 1.25rem; font-weight: 400; }
details.card > summary h3 { display: inline; }
details.card > summary .one-line { color: var(--fail); font-family: ui-monospace, Consolas, monospace; font-size: .85rem;
  margin-top: .3rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
details.card[open] > summary { border-bottom: 1px solid var(--line); }
details.card[open] > summary .one-line { display: none; }
details.card > .card-body { padding: .25rem 1.25rem 1rem; }
details.group > summary { font-weight: 400; }
details.group > summary .headline { margin: .4rem 0 0; }
.fold-tools { display: flex; gap: .5rem; margin: .25rem 0 .75rem; }
.fold-tools button { font: inherit; padding: .2rem .7rem; border: 1px solid var(--line); border-radius: 4px;
  background: var(--card); color: var(--fg); cursor: pointer; }
ul.verdict { padding-left: 1.25rem; }
.product { font-weight: 600; color: var(--fg); }
ul.verdict .product { font-weight: 400; color: var(--muted); margin-left: .5rem; font-size: .95rem; }
.history { display: inline-flex; align-items: center; gap: .5rem; }
.strip { display: inline-flex; gap: 2px; }
.blk { width: 9px; height: 14px; border-radius: 2px; background: var(--line); display: inline-block; }
.blk.passed, .blk.xpassed { background: var(--pass); }
.blk.failed, .blk.error { background: var(--fail); }
.blk.skipped, .blk.xfailed { background: var(--skip); }
.group { border: 1px dashed var(--fail); border-radius: 6px; padding: 1rem 1.25rem; margin-bottom: 1.25rem; }
.group-head { margin-bottom: 1rem; }
.group-head b { font-size: 1.05rem; margin-right: .5rem; }
h4.module { position: sticky; top: 0; z-index: 1; margin: 1.75rem 0 .25rem; padding: .5rem .75rem;
  background: var(--card); border-left: 4px solid var(--link); border-bottom: 1px solid var(--line);
  text-transform: none; letter-spacing: 0; font-family: ui-monospace, Consolas, monospace; font-size: .95rem; }
input#filter { width: min(100%, 36rem); padding: .45rem .7rem; font: inherit; border: 1px solid var(--line); border-radius: 6px; background: var(--card); color: var(--fg); }
#filter-count { color: var(--muted); margin-left: .75rem; font-size: .9rem; }
table.facts-table { border-collapse: collapse; width: 100%; max-width: 60rem; }
table.facts-table th, table.facts-table td { text-align: left; padding: .35rem .6rem; border-bottom: 1px solid var(--line); vertical-align: top; }
table.facts-table th { color: var(--muted); font-weight: 600; font-size: .85rem; }
table.facts-table td.num { text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; }
'''



def filter_script(noun: str = 'tests') -> str:
    """The filter box's script: every element with a data-test attribute
    stays visible while that attribute holds the typed text. `noun` is
    the word of the match count ("3 of 49 tests match"). The archive
    index reuses it for its run rows."""
    return '''
(function () {
  var box = document.getElementById('filter');
  var count = document.getElementById('filter-count');
  if (!box) { return; }
  var items = document.querySelectorAll('[data-test]');
  var sections = document.querySelectorAll('details.section');
  var groups = document.querySelectorAll('details.group, details.watch-module');
  box.addEventListener('input', function () {
    var q = box.value.trim().toLowerCase();
    var shown = 0;
    for (var i = 0; i < items.length; i++) {
      var hit = !q || items[i].getAttribute('data-test').indexOf(q) !== -1;
      items[i].style.display = hit ? '' : 'none';
      if (hit) { shown++; }
    }
    // A match hidden inside a collapsed list is no match at all.
    for (var j = 0; j < sections.length && q; j++) { sections[j].open = true; }
    // The same for a folded group: open it when it holds a match, and hide it when it holds none.
    for (var k = 0; k < groups.length; k++) {
      var inside = groups[k].querySelectorAll('[data-test]');
      var hits = 0;
      for (var m = 0; m < inside.length; m++) { if (inside[m].style.display !== 'none') { hits++; } }
      groups[k].style.display = (!q || hits || !inside.length) ? '' : 'none';
      if (q && hits) { groups[k].open = true; }
    }
    count.textContent = q ? shown + ' of ' + items.length + ' ''' + noun + ''' match' : '';
  });
})();
'''


_FILTER_JS = filter_script()


def _href(rel: str) -> str:
    return quote(rel, safe='/')


def run_page(name: str, runs_dir: Path | None) -> str:
    """The file a link to an archived run should open, relative to its
    folder: 'run.html' when the folder has one, else 'report.html', else
    '' for the folder itself. Without a runs_dir to look in (pure
    rendering), or for a folder that is not there, run.html: the page
    every archive gets."""
    if runs_dir is None:
        return 'run.html'
    folder = Path(runs_dir) / name
    if not folder.is_dir() or (folder / 'run.html').is_file():
        return 'run.html'
    if (folder / 'report.html').is_file():
        return 'report.html'
    return ''


def run_href(name: str, runs_dir: Path | None = None, prefix: str = '') -> str:
    """The href of an archived run from a page that reaches the runs
    folder through `prefix` ('../' from a run folder, '' from the folder
    itself). See run_page for which file it opens."""
    return f'{prefix}{_href(str(name))}/{run_page(str(name), runs_dir)}'


@dataclass(frozen=True)
class RunLinks:
    """How a page links archived runs: the runs folder on disk, and the
    run whose page is being built. That run's run.html is not on disk
    until the builder's caller writes it, so it always links run.html."""

    runs_dir: Path | None = None
    current: str | None = None

    def href(self, name: str, prefix: str = '') -> str:
        if str(name) == self.current:
            return f'{prefix}{_href(str(name))}/run.html'
        return run_href(name, self.runs_dir, prefix)


def _headline(log: str) -> str:
    """The first block of pytest's 'E ' lines - the error before the traceback."""
    lines = []
    for raw in log.splitlines():
        m = _ERROR_LINE_RE.match(raw)
        if m:
            lines.append(m.group(1))
            if len(lines) >= _HEADLINE_MAX:
                break
        elif lines:
            break
    return '\n'.join(lines).strip()


def _render_shots(rec: TestRecord, compact: bool = False) -> str:
    """The final screenshots, plus the mid-drag frames on a failure only:
    a passed drag's frames are on disk for anyone who wants them, but on
    the page they are noise."""
    items = [(rel, 'final') for rel in rec.screenshots]
    if rec.outcome in FAILURE_OUTCOMES:
        items += [(rel, 'mid-drag') for rel in rec.frames]
    if not items:
        return ''
    figures = []
    labels = {'final': 'Final screenshot', 'mid-drag': 'Mid-drag frame'}
    for rel, kind in sorted(items, key=lambda i: (i[1] == 'final', i[0])):
        name = Path(rel).name
        # the file name is on hover and in the link; the caption reads as words
        caption = '' if compact else f'<figcaption>{escape(labels[kind])}</figcaption>'
        figures.append(
            f'<figure><a href="{_href(rel)}" target="_blank" title="{escape(name, quote=True)}">'
            f'<img src="{_href(rel)}" alt="{escape(labels[kind])}" loading="lazy"></a>'
            f'{caption}</figure>')
    return '<div class="shots">' + ''.join(figures) + '</div>'


def _render_actions(digest: dict) -> str:
    items = []
    for action in digest.get('actions') or []:
        line = escape(f"{action.get('name', '')} {action.get('params', '')}".strip())
        cls = ' class="failing"' if action.get('error') else ''
        extra = ''
        if action.get('error'):
            extra += f'<span class="err">ERROR: {escape(str(action["error"]).splitlines()[0])}</span>'
            for log_line in action.get('log') or []:
                extra += f'<span class="log">{escape(log_line)}</span>'
        items.append(f'<li{cls}>{line}{extra}</li>')
    return '<ul class="actions">' + ''.join(items) + '</ul>' if items else ''


def _filter_key(rec: TestRecord) -> str:
    """What the filter box matches against: every name the test goes by."""
    words = (rec.display_title, rec.nodeid, rec.case_key, rec.case_title,
             rec.module_name, rec.outcome, rec.delta, rec.product)
    return escape(' '.join(w for w in words if w).lower(), quote=True)


def _delta_chip(rec: TestRecord) -> str:
    if not rec.delta:
        return ''
    return (f'<span class="chip delta {escape(rec.delta.lower().replace(" ", "-"))}">'
            f'{escape(rec.delta)}</span>')


def _seen_chip(rec: TestRecord, links: RunLinks | None = None) -> str:
    """New error, or Seen before with the newest earlier run that showed
    the same error signature, linked."""
    if rec.outcome not in FAILURE_OUTCOMES or not rec.signature:
        return ''
    if rec.seen is None:
        return '<span class="chip delta new-error">New error</span>'
    when = friendly_time(rec.seen.get('started'))
    href = (links or RunLinks()).href(rec.seen['run'], '../')
    return (f'<span class="chip delta seen-before">Seen before (last on '
            f'<a href="{href}">{escape(when)}</a>)</span>')


def render_strip(facts: Mapping, href_prefix: str = '', with_text: bool = True,
                 links: RunLinks | None = None, clock: bool = False) -> str:
    """The history strip: one block per run, oldest left and newest right,
    each a link to that run's page with the date and outcome as its
    tooltip. With `with_text`, "passed 17/20" follows, then the flaky
    label with the flaky rate when the window holds both outcomes.
    `href_prefix` is the path from the page to the runs folder, and
    `links` says which file each run has to open."""
    if not facts or not facts.get('runs'):
        return ''
    links = links or RunLinks()
    from soterix_qa_shared.reporting.run_page import utc_iso

    def stamp(block: Mapping) -> str:
        utc = utc_iso(block.get('started'), block.get('utc_offset'))
        # `clock`: the run page's script shows each tooltip on the reader's clock. The health page has no clock switch.
        return f' data-utc="{utc}" data-label="{escape(outcome_label(block["outcome"]), quote=True)}"' if clock and utc else ''
    blocks = ''.join(
        f'<a class="blk {escape(str(b["outcome"]))}" href="{links.href(b["run"], href_prefix)}"{stamp(b)} '
        f'title="{escape(friendly_time(b.get("started")))}: {escape(outcome_label(b["outcome"]))}"></a>'
        for b in facts['blocks'])
    parts = [(f'<span class="strip" title="the last {facts["runs"]} runs of these tests, '
             f'newest right">{blocks}</span>')]
    if with_text:
        parts.append(f'<span>Passed {facts["passed"]}/{facts["runs"]}</span>')
        if facts['flaky']:
            parts.append(f'<span class="chip delta flaky">Flaky {facts["rate"] * 100:.0f}%</span>')
    return '<span class="history">' + ''.join(parts) + '</span>'


def short_id(nodeid: str) -> str:
    """The node id without its file: TestClass::test_name[param]. The
    module heading above a row already names the file."""
    return nodeid.split('::', 1)[1] if '::' in nodeid else nodeid


def _render_identity(rec: TestRecord, heading: str, compact: bool = False) -> str:
    """Title first, QA Touch case second, node id smaller beneath. Compact
    (the passed and skipped rows): the case and the short id share one
    muted line, the full id on hover."""
    parts = [f'<{heading} class="title">{escape(rec.display_title)}</{heading}>']
    case = ''
    if rec.case_key or rec.case_title:
        case = f'QA Touch {rec.case_key}'.rstrip() + (f': {rec.case_title}' if rec.case_title else '')
    if compact:
        bits = [f'<span class="case">{escape(case)}</span>'] if case else []
        # the short id is what shows; the full id follows in a clipped span
        # so a copy or a screen reader still gets it whole
        bits.append(f'<span class="nodeid" title="{escape(rec.nodeid, quote=True)}">'
                    f'{escape(short_id(rec.nodeid))}'
                    f'<span class="sr-only"> ({escape(rec.nodeid)})</span></span>')
        parts.append('<div class="sub">' + ''.join(bits) + '</div>')
        return ''.join(parts)
    if case:
        parts.append(f'<div class="case">{escape(case)}</div>')
    parts.append(f'<div class="nodeid">{escape(rec.nodeid)}</div>')
    return ''.join(parts)


def _render_facts(rec: TestRecord, with_outcome: bool = False, with_seen: bool = True,
                  links: RunLinks | None = None, with_product: bool = False,
                  with_module: bool = True) -> str:
    """The fact line under a test's names. `with_product` is on when the
    run mixes products: the header then names no single one, so each
    test names its own."""
    facts = []
    if with_outcome:
        facts.append(f'<span class="chip {escape(rec.outcome)}">{escape(outcome_label(rec.outcome))}</span>')
    if rec.duration_text:
        facts.append(f'<span>{escape(rec.duration_text)}</span>')
    facts.append(_delta_chip(rec))
    if with_seen:
        facts.append(_seen_chip(rec, links))
    if with_product and rec.product:
        facts.append(f'<span class="product">Product: {escape(rec.product)}</span>')
    if with_module:
        facts.append(f'<span>{escape(rec.module_name)}</span>')
    if rec.log_file:
        facts.append(f'<a href="{_href(rec.log_file)}">captured output</a>')
    facts.append(render_strip(rec.history, '../', links=links))
    return '<div class="facts">' + ''.join(f for f in facts if f) + '</div>'


def failure_digest(rec: TestRecord, run_dir: Path) -> dict | None:
    """The digest of the test's first trace zip, or None without one."""
    trace_zips = [rel for rel in rec.traces if rel.lower().endswith('.zip')]
    return digest_trace(run_dir / trace_zips[0]) if trace_zips else None


def failure_headline(rec: TestRecord, digest: dict | None) -> str:
    """The error block from the captured log, else the trace's error lines."""
    headline = _headline(rec.log)
    if not headline and digest and digest.get('errors'):
        headline = '\n'.join(digest['errors'])
    return headline or rec.error


# A run with more failures than this shows each one folded to its title, its facts and
# the first line of its error. A run with 3 failures is read from top to bottom. A run
# with 24 was a long scroll for each failure (Justin, 09/21/2026).
OPEN_FAILURES_MAX = 3


def _render_failure(rec: TestRecord, run_dir: Path, with_seen: bool = True,
                    with_product: bool = False, links: RunLinks | None = None,
                    folded: bool = False) -> str:
    # build_run_report fills these in. A caller that skipped it still gets a card.
    digest = rec.digest if rec.digest is not None else failure_digest(rec, run_dir)
    headline = rec.headline or failure_headline(rec, digest)
    one_line = (f'<div class="one-line">{escape(first_line(headline))}</div>' if headline else '')
    parts = [(f'<details class="card {escape(rec.outcome)}" id="{escape(rec.safe, quote=True)}" '
             f'data-test="{_filter_key(rec)}"{"" if folded else " open"}>'),
             '<summary>' + _render_identity(rec, 'h3'),
             _render_facts(rec, with_outcome=True, with_seen=with_seen,
                           links=links, with_product=with_product) + one_line + '</summary>',
             '<div class="card-body">']
    parts.append('<h4>Error</h4>')
    if headline:
        parts.append(f'<pre class="headline">{escape(headline)}</pre>')
    else:
        parts.append('<p class="empty">No error message recorded.</p>')
    if rec.log:
        parts.append(f'<details><summary>Full traceback and captured output</summary>'
                     f'<pre>{escape(rec.log)}</pre></details>')

    if digest is not None:
        parts.append(f'<h4>Playwright actions (last {LAST_ACTIONS}, failing one last)</h4>')
        if digest.get('url'):
            parts.append(f'<p>Final URL: <code>{escape(str(digest["url"]))}</code></p>')
        parts.append(_render_actions(digest) or '<p class="empty">No actions in the trace.</p>')
        console = digest.get('console') or []
        parts.append(f'<h4>Console errors ({len(console)})</h4>')
        if console:
            parts.append('<ul class="plain">' + ''.join(
                f'<li><code>{escape(c)}</code></li>' for c in console[:20]) + '</ul>')
        else:
            parts.append('<p class="empty">None.</p>')

    if rec.advisories:
        parts.append(f'<h4 title="{escape(NOTES_TITLE, quote=True)}">Notes</h4><ul class="plain">'
                     + ''.join(f'<li>{escape(a)}</li>' for a in rec.advisories) + '</ul>')

    parts.append('<h4>Screenshots</h4>')
    parts.append(_render_shots(rec) or '<p class="empty">No screenshot for this test.</p>')

    parts.append('<h4>Video</h4>')
    if rec.videos:
        for rel in rec.videos:
            parts.append(f'<video controls preload="metadata" src="{_href(rel)}"></video>'
                         f'<div><a href="{_href(rel)}" title="{escape(Path(rel).name, quote=True)}">Video clip</a></div>')
    else:
        parts.append('<p class="empty">No video kept for this test.</p>')

    parts.append('<h4>Trace</h4>')
    if rec.traces:
        for rel in rec.traces:
            absolute = os.path.abspath(run_dir / rel)
            label = 'Trace zip' if rel.lower().endswith('.zip') else Path(rel).name
            parts.append(f'<div><a href="{_href(rel)}" title="{escape(Path(rel).name, quote=True)}">'
                         f'{escape(label)}</a></div>')
            if rel.lower().endswith('.zip'):
                parts.append(f'<pre>python -m playwright show-trace "{escape(absolute)}"</pre>')
    else:
        parts.append('<p class="empty">No trace kept for this test.</p>')
    parts.append('</div></details>')
    return '\n'.join(parts)


def _render_group(group: list[TestRecord], run_dir: Path, with_product: bool = False,
                  links: RunLinks | None = None, folded: bool = False) -> str:
    """One failure renders as its card. Several with one error signature
    share a frame that says so, with the error and its new/seen label
    once at the top instead of on every card. With `folded`, the frame and
    its cards start closed: the frame's top line is the error they share."""
    if len(group) == 1:
        return _render_failure(group[0], run_dir, with_product=with_product, links=links, folded=folded)
    lead = group[0]
    head = (f'<summary class="group-head"><b>{len(group)} tests share this error</b>'
            f'{_seen_chip(lead, links)}'
            f'<pre class="headline">{escape(first_line(lead.headline))}</pre></summary>')
    cards = '\n'.join(_render_failure(r, run_dir, with_seen=False, with_product=with_product,
                                      links=links, folded=True) for r in group)
    return f'<details class="group"{"" if folded else " open"}>{head}\n{cards}\n</details>'


def _render_row(rec: TestRecord, links: RunLinks | None = None,
                with_product: bool = False) -> str:
    extras = ''
    if rec.advisories:
        extras += '<ul class="plain">' + ''.join(
            f'<li>{escape(a)}</li>' for a in rec.advisories) + '</ul>'
    return (f'<div class="row" data-test="{_filter_key(rec)}"><div class="row-main">'
            f'{_render_identity(rec, "div", compact=True)}'
            f'{_render_facts(rec, links=links, with_product=with_product, with_module=False)}'
            f'{extras}</div>{_render_shots(rec, compact=True)}</div>')


def _render_collapsed(title: str, records: list[TestRecord], links: RunLinks | None = None,
                      with_product: bool = False) -> str:
    """A collapsed list, its tests grouped under their module."""
    if records:
        groups: dict[str, list[TestRecord]] = {}
        for rec in records:
            groups.setdefault(rec.module_name, []).append(rec)
        # each module in its own wrapper, so its sticky heading stays
        # only while its rows scroll and the next heading takes over
        body = ''.join(
            f'<div class="module-group"><h4 class="module">{escape(module)} ({len(group)})</h4>'
            + ''.join(_render_row(r, links, with_product) for r in group) + '</div>'
            for module, group in sorted(groups.items()))
    else:
        body = '<p class="empty">None.</p>'
    return (f'<details class="section"><summary>{escape(title)} ({len(records)})</summary>'
            f'{body}</details>')


def _render_slowest(records: list[TestRecord], count: int = SLOWEST_COUNT) -> str:
    timed = sorted((r for r in records if r.seconds is not None),
                   key=lambda r: r.seconds or 0.0, reverse=True)[:count]
    if not timed:
        return '<p class="empty">No per-test timing in this run.</p>'
    rows = ''.join(
        f'<tr><td class="num">{escape(format_seconds(r.seconds or 0.0))}</td>'
        f'<td>{escape(r.display_title)}<div class="nodeid">{escape(r.nodeid)}</div></td>'
        f'<td>{escape(outcome_label(r.outcome))}</td></tr>' for r in timed)
    return ('<table class="facts-table"><tr><th>Duration</th><th>Test</th><th>Outcome</th></tr>'
            + rows + '</table>')


def summary_product(summary: Mapping) -> str:
    """The run's product from summary.json: provenance's label, else (an
    archive before schema 4) the label its per-test records' node ids
    derive, less any marker, else '' (no records either)."""
    prov = summary.get('provenance') if isinstance(summary.get('provenance'), dict) else {}
    recorded = str(prov.get('product') or '')
    if recorded:
        return recorded
    return run_product(str(r.get('product') or '') or product_name(product_for(str(r['nodeid'])))
                       for r in summary_tests(summary))


def _render_provenance(summary: dict, product: str = '') -> str:
    """The provenance line: the product first, as a name, then the
    machine facts in code."""
    prov = summary.get('provenance') if isinstance(summary.get('provenance'), dict) else {}
    # Names read as names: Chromium / Chrome, Playwright, Env QA
    browser = ' / '.join(str(v).capitalize() for v in (prov.get('browser'), prov.get('browser_channel')) if v)
    if prov.get('browser_version'):
        browser = f"{browser} {prov['browser_version']}".strip()
        latest = prov.get('browser_latest')
        if latest and str(latest) != str(prov['browser_version']):
            browser += f' (stable {latest} is out)'
    versions = ', '.join(
        f'{label} {prov[key]}' for label, key in
        (('Playwright', 'playwright'), ('Pytest-Playwright', 'pytest_playwright')) if prov.get(key))
    env = str(prov.get('env')).upper() if prov.get('env') else None
    facts = (('Env', env), ('Box', prov.get('box')), ('Tenant', prov.get('base_url_host')),
             ('Machine', prov.get('host')), ('Operator', prov.get('operator')),
             ('Account', prov.get('account')),
             ('Commit', prov.get('git_commit')), ('Browser', browser), ('Versions', versions))
    items = [_fact('Product', escape(product), lead=True)] if product else []
    for label, value in facts:
        if not value:
            continue
        text = escape(str(value))
        if label == 'Browser' and ' (stable ' in text:   # the update note reads as a note, not as the version
            head, _, tail = text.partition(' (stable ')
            text = f'{head} <span class="note">(stable {tail}</span>'
        items.append(_fact(label, text))
    return ''.join(items)


def _fact(label: str, value_html: str, lead: bool = False, tone: str = '') -> str:
    """One labelled fact of the run header: a small muted label above a bold value.

    The header was 2 lines of 'Label: value' pairs in one colour, and a reader could not
    find the box or the machine in it (Justin, 09/21/2026). `tone` is 'pass' or 'fail'
    for the outcome: a coloured word with a dot before it."""
    classes = 'fact' + (' lead' if lead else '') + (f' {tone}' if tone else '')
    return f'<div class="{classes}"><dt>{escape(label)}</dt><dd>{value_html}</dd></div>'


def _outcome_tone(exitstatus: object) -> str:
    if exitstatus is None:
        return ''
    return 'pass' if exitstatus == 0 else 'fail'


def _read_first_html(summary: Mapping[str, Any]) -> str:
    """The lines to read before the failure list: one cause shared by most
    failures, one reason shared by most skips, and how much of the plan
    never ran. Empty when none applies."""
    lines = []
    cause = summary.get('one_cause') if isinstance(summary.get('one_cause'), dict) else None
    if cause:
        lines.append(f"One cause, {cause.get('count')} of {cause.get('of')} failures: {cause.get('line')} "
                     f"(first: {cause.get('first')}). Fix this before reading the rest.")
    skip = summary.get('one_skip_reason') if isinstance(summary.get('one_skip_reason'), dict) else None
    if skip:
        lines.append(f"{skip.get('count')} tests skipped for one reason: {skip.get('reason')}")
    not_run = int(summary.get('not_run_count') or 0)
    if not_run:
        lines.append(f"{not_run} of {summary.get('collected')} collected tests did not run; "
                     f"the counts cover only what ran.")
    if not lines:
        return ''
    return '<ul class="read-first">' + ''.join(f'<li>{escape(line)}</li>' for line in lines) + '</ul>'


def render_run_report(run_dir: Path, summary: dict, tests: dict[str, TestRecord],
                      unattached: list[str], previous: Mapping | None = None,
                      verdict: str = '', product: str = '') -> str:
    """Render the page from already-collected facts. Disk reads stop at
    which files the run folder and its neighbours hold (for the links)
    and a failure card whose digest build_run_report did not fill.
    `previous` is the run the change labels were computed against
    ({'name', 'started'}), when there is one. `product` is the run's
    product label: one name, or "Mixed: A, B", or ''.

    The page is run_page.render_run_page since 09/21/2026: the verdict, the causes and
    the map, then 1 test in a side pane. `verdict` is not used by it: the page counts
    the records it shows. render_run_report_classic is the page before it."""
    from soterix_qa_shared.reporting.run_page import render_run_page
    return render_run_page(run_dir, summary, tests, unattached, previous=previous, product=product)


def render_run_report_classic(run_dir: Path, summary: dict, tests: dict[str, TestRecord],
                              unattached: list[str], previous: Mapping | None = None,
                              verdict: str = '', product: str = '') -> str:
    """The run page as it was until 09/21/2026: each failure as a card, in one long column.
    Kept for its pins, which hold the rendering helpers that the health page shares."""
    counts = summary.get('counts') if isinstance(summary.get('counts'), dict) else {}
    target = ' '.join(str(t) for t in (summary.get('target') or [])) or run_dir.name.partition('__')[2]
    started = summary.get('started') or run_dir.name.partition('__')[0].replace('_', ' ')
    duration = summary.get('duration_s')
    exitstatus = summary.get('exitstatus')
    runs_dir = run_dir.parent
    run_links = RunLinks(runs_dir, run_dir.name)
    # A mixed run's header names no single product, so every test names its own.
    with_product = is_mixed(product)
    when = friendly_stamp(started)
    title = f'{product} - Test Run - {when}' if product else f'Test Run - {when}'

    def by_outcome(names: tuple[str, ...]) -> list[TestRecord]:
        return sorted((t for t in tests.values() if t.outcome in names),
                      key=lambda t: (t.module_name, t.nodeid))

    failures = by_outcome(FAILURE_OUTCOMES)
    passed = by_outcome(PASSED_OUTCOMES)
    skipped = by_outcome(SKIPPED_OUTCOMES)
    unknown = by_outcome(('unknown',))

    chips = ''.join(
        f'<span class="chip {key}">{escape(key.capitalize())}: {escape(str(counts.get(key, "-")))}</span>'
        for key in ('passed', 'failed', 'skipped', 'errors'))
    links = ['<a href="../index.html">Archive Index</a>']
    if (run_dir / 'report.html').is_file():
        links.insert(0, '<a href="report.html">Raw pytest-html</a>')
    if (runs_dir / 'health.html').is_file():
        links.append('<a href="../health.html">Health</a>')
    if (run_dir / 'advisories.txt').is_file():
        links.append('<a href="advisories.txt">advisories.txt</a>')
    has_run_log = (run_dir / 'log' / 'run.log').is_file()
    if has_run_log:
        links.append('<a href="log/run.log">run.log</a>')
    if previous:
        links.append(f'<a href="{run_href(previous["name"], runs_dir, "../")}">'
                     'Previous run of these tests</a>')
    links.append('<a href="./">Folder</a>')

    if previous:
        def link(label: str) -> str:
            return f'<a href="{run_href(previous["name"], runs_dir, "../")}">{escape(label)}</a>'
        compared = (f'<p class="case">{delta_wording(previous, link)}. Each changed test carries '
                    f'a label: {DELTA_NEW_FAILURE}, {DELTA_FIXED}, {DELTA_STILL_FAILING}, '
                    f'or {DELTA_NEW_TEST}.</p>')
    else:
        compared = f'<p class="case">{escape(delta_wording(None))}</p>'
    verdict_html = f'<p class="verdict">{escape(verdict)}</p>' if verdict else ''
    verdict_html += _read_first_html(summary)

    advisories = [a for a in (summary.get('advisories') or []) if isinstance(a, dict)]
    if advisories:
        adv_body = '<ul class="plain">' + ''.join(
            f"<li><code>{escape(str(a.get('test', '')))}</code>: {escape(str(a.get('line', '')))}</li>"
            for a in advisories) + '</ul>'
    else:
        adv_body = '<p class="empty">None.</p>'

    fold = len(failures) > OPEN_FAILURES_MAX
    fold_tools = ('<div class="fold-tools">'
                  '<button type="button" onclick="document.querySelectorAll(\'#failure-list details.card, '
                  '#failure-list details.group\').forEach(d => d.open = true)">Expand all</button>'
                  '<button type="button" onclick="document.querySelectorAll(\'#failure-list details.card, '
                  '#failure-list details.group\').forEach(d => d.open = false)">Collapse all</button>'
                  '<span class="case">Click a failure to open it.</span></div>' if fold else '')
    failure_body = (fold_tools + '<div id="failure-list">'
                    + '\n'.join(_render_group(g, run_dir, with_product, run_links, folded=fold)
                                for g in group_failures(failures)) + '</div>'
                    if failures else '<p class="empty">No failures in this run.</p>')
    passed_note = ('<p class="case">The captured output of passed and skipped tests is in '
                   '<a href="log/run.log">log/run.log</a>, one section per test.</p>'
                   if has_run_log else '')

    extra_sections = ''
    if unknown:
        extra_sections += _render_collapsed('Outcome unknown', unknown, run_links, with_product)
    if unattached:
        extra_sections += ('<details class="section"><summary>Files not matched to a test '
                           f'({len(unattached)})</summary><ul class="plain">'
                           + ''.join(f'<li><a href="{_href(u)}">{escape(u)}</a></li>'
                                     for u in unattached) + '</ul></details>')

    return f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
{favicon_link()}
<style>
.brand-row {{ display: flex; align-items: center; gap: 1rem; margin-bottom: 0.5rem; }}
.brand-row h1 {{ margin: 0; }}
.brand {{ display: block; }}{PAGE_CSS}</style>
</head>
<body>
<div class="brand-row">{header_logo()}<h1>Test Run - {escape(friendly_stamp(started))}</h1></div>
{verdict_html}
<div class="ledger"><dl class="run-facts">
{_fact('Outcome', outcome_html(exitstatus), lead=True, tone=_outcome_tone(exitstatus))}{_fact('Target', escape(target))}{_fact('Duration', escape(duration_text(duration)))}{_render_provenance(summary, product)}{_fact('Folder', escape(run_dir.name))}
</dl></div>
<div class="chips">{chips}</div>
<div class="meta">{' | '.join(links)}</div>
{compared}
<p><input id="filter" type="search" placeholder="Filter tests: title, node id, case key, module, outcome"
 aria-label="Filter tests"><span id="filter-count"></span></p>

<h2 id="failures">Failures ({len(failures)})</h2>
{failure_body}

<h2 id="advisories" title="{escape(NOTES_TITLE, quote=True)}">Notes ({len(advisories)})</h2>
{adv_body}

<h2 id="slowest">Slowest {SLOWEST_COUNT}</h2>
{_render_slowest(list(tests.values()))}

<h2 id="passed">Passed</h2>
{passed_note}
{_render_collapsed('Passed', passed, run_links, with_product)}

<h2 id="skipped">Skipped</h2>
{_render_collapsed('Skipped', skipped, run_links, with_product)}
{extra_sections}
<script>{_FILTER_JS}</script>
</body>
</html>
'''


def build_run_report(run_dir: Path) -> str:
    """Build the run.html for one archived run folder and return it.

    Reads the folder, then the previous HISTORY_RUNS - 1 archives of the
    same target for the change labels, the history strips, and the
    new-or-seen verdict on each error signature.
    """
    run_dir = Path(run_dir)
    summary = load_summary(run_dir)
    tests, unattached = collect_tests(run_dir)
    # Archives before schema 4 recorded no product. The node id still
    # answers (path rule, environment, default), less any marker.
    for rec in tests.values():
        if not rec.product:
            rec.product = product_name(product_for(rec.nodeid))
    product = summary_product(summary) or run_product(r.product for r in tests.values())
    outcomes = {nodeid: rec.outcome for nodeid, rec in tests.items()}
    history = load_history(run_dir)
    previous = history[0] if history else None
    chips = compute_deltas(outcomes, previous['outcomes'] if previous else None)
    for nodeid, chip in chips.items():
        tests[nodeid].delta = chip

    current = {'name': run_dir.name, 'started': run_started(run_dir, summary),
               'utc_offset': summary.get('utc_offset'), 'outcomes': outcomes}
    runs = [current, *history]
    for nodeid, rec in tests.items():
        rec.history = history_facts(test_results(nodeid, runs))

    seen = signatures_seen(history)
    for rec in tests.values():
        if rec.outcome not in FAILURE_OUTCOMES:
            continue
        rec.digest = failure_digest(rec, run_dir)
        rec.headline = failure_headline(rec, rec.digest)
        rec.signature = error_signature(rec.headline)
        rec.seen = seen.get(rec.signature) if rec.signature else None

    counts = summary.get('counts') if isinstance(summary.get('counts'), dict) else None
    if counts:
        passed, skipped = int(counts.get('passed', 0) or 0), int(counts.get('skipped', 0) or 0)
        failed = int(counts.get('failed', 0) or 0) + int(counts.get('errors', 0) or 0)
    else:
        # No summary (an older layout, an archive that died mid-write): the
        # collected records are the only count there is.
        passed = sum(1 for o in outcomes.values() if o in PASSED_OUTCOMES)
        failed = sum(1 for o in outcomes.values() if o in FAILURE_OUTCOMES)
        skipped = sum(1 for o in outcomes.values() if o in SKIPPED_OUTCOMES)
    verdict = run_verdict(passed, failed, skipped, chips,
                          not_run=int(summary.get('not_run_count') or 0),
                          reruns=sum(1 for t in tests.values() if t.reruns and t.outcome in PASSED_OUTCOMES))
    return render_run_report(run_dir, summary, tests, unattached, previous=previous,
                             verdict=verdict, product=product)


# ---------------------------------------------------------------- CLI

def _run_dirs(runs_dir: Path) -> list[Path]:
    """The run folders under runs_dir, oldest first."""
    if not runs_dir.is_dir():
        return []
    return sorted(p for p in runs_dir.iterdir() if p.is_dir() and RUN_DIR_RE.match(p.name))


def _resolve_run_dir(arg: str) -> Path | None:
    """'latest' is the newest run folder under reports/runs; else the path."""
    if arg != 'latest':
        path = Path(arg)
        return path if path.is_dir() else None
    from soterix_qa_shared.reporting.run_archive import RUNS_DIR
    runs = _run_dirs(RUNS_DIR)
    return runs[-1] if runs else None


def backfill_run_reports(runs_dir: Path, out=None) -> tuple[list[Path], list[Path]]:
    """Write run.html for every archived run under runs_dir that has none.

    Oldest first, so each page's history sees the pages before it. A
    folder whose page cannot be built is reported and skipped: one
    broken archive must not stop the rest. Returns the folders written
    and the folders that failed.
    """
    out = out or sys.stdout
    written: list[Path] = []
    failed: list[Path] = []
    for run_dir in _run_dirs(runs_dir):
        page = run_dir / 'run.html'
        if page.is_file():
            continue
        try:
            page.write_text(build_run_report(run_dir), encoding='utf-8')
        except Exception as e:  # noqa: BLE001 - the other folders still get their pages
            failed.append(run_dir)
            out.write(f'could not build {page}: {e}\n')
            continue
        written.append(run_dir)
        out.write(f'{page}\n')
    return written, failed


def _backfill_all() -> int:
    """`--all`: the missing pages, then the index, which flags who has one."""
    from soterix_qa_shared.reporting.run_archive import RUNS_DIR, _write_index
    written, failed = backfill_run_reports(RUNS_DIR)
    _write_index(RUNS_DIR)
    sys.stdout.write(f'{len(written)} run.html written, {len(failed)} failed, '
                     f'{len(_run_dirs(RUNS_DIR)) - len(written) - len(failed)} already had one\n')
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1 or argv[0] in ('-h', '--help'):
        sys.stderr.write('usage: python -m plugins.run_report <run-dir|latest|--all>\n')
        return 2
    if argv[0] == '--all':
        return _backfill_all()
    run_dir = _resolve_run_dir(argv[0])
    if run_dir is None:
        sys.stderr.write(f'No run folder at {argv[0]}. Archived runs live under reports/runs/.\n')
        return 1
    out = run_dir / 'run.html'
    out.write_text(build_run_report(run_dir), encoding='utf-8')
    sys.stdout.write(f'{out}\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
