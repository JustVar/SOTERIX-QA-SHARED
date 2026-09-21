"""Per-run archive -- pytest plugin.

Copies each run's evidence into its own folder before the next run can
destroy it: reports/report.html is overwritten every run, pytest-playwright
wipes reports/artifacts/ at session start, and reports/screenshots/ only
ever holds the latest run.

What lands in reports/runs/<start-time>__<target>/:
  - run.html: the run's own page (plugins/run_report.py) - failures first,
    each with its error, call log, screenshots, video, and trace command.
  - report.html: this run's HTML report (skipped when the run wrote none).
  - screenshots/: the labeled per-test screenshots (see report_media).
  - traces/: everything under reports/artifacts/ - failure trace zips plus
    pytest-playwright's per-test artifact dirs.
  - last_run_results.json: the per-nodeid outcomes from qatouch_reporter,
    when that reporter ran.
  - log/: run.log, always, with every INFO-or-higher logging record of
    the session and then, one section per test, the stdout and stderr
    pytest captured in every phase of each passed and skipped test. Each
    failed or errored test gets its own file beside it, with the same
    captured output plus the traceback: those are the files a triage
    opens, and one file per passing test buried them.
  - summary.json: timing, counts, exit status, the invocation target,
    provenance (ENV, base_url host, git commit, browser, Playwright
    versions, and the run's product: one name, or "Mixed: A, B"), the
    advisories below, and one record per test: node id, module, readable
    title (the docstring's first line, else the function name as words),
    the QA Touch case from config/qatouch_case_mapping.json when the node
    id is mapped, the product (plugins/products.py: marker, then path
    rule, then the mapping file's project_key, then the environment),
    outcome, per-phase timing, its log file (failures only), and the
    error line of a failure. The node id is the key. The title is
    display only.
  - advisories.txt: every [VIEWS] / [LPR] / [EXPORT] marker line a test
    printed, grouped by test - the drag fallbacks, content-health deltas,
    and export timings the page objects report. They come from pytest's
    captured output, so a -s run shows them live instead of filing them.
    The same lines print as a 'run advisories' section at the end of
    every run, so nobody has to grep for them.

Start at reports/runs/index.html: one row per archived run, newest first:
when it started, its product (the run's, else derived from its targets,
never blank), its target, the counts, its outcome (the exit status as a
word, with the code as a tooltip), its notes count (the advisories), and
links to each run's page (Run Report), its pytest-html file (Raw
pytest-html), and its folder, and to health.html (tools/health_summary.py,
`python run.py health`) when that page exists. A filter box narrows the
rows to those that hold the typed text.
The index is rebuilt after every run from the folders' summary.json
files, so hand-deleting a folder is safe.

Runs whose every target sits under tests/plugins or tests/tools are not
archived: those are the repo's own unit tests, and they leave no UI
evidence worth a folder.

Retention: each target keeps its newest 20 runs, capped at 100 runs in
total - per-target so a morning of debug iterations on one file can only
evict its own older runs, never the last full-suite archive. Only folders
named like <YYYY-MM-DD_HH-MM-SS>__<target> are ever pruned; index.html and
anything else parked in reports/runs/ stays.

Everything is a copy. The live files are never moved or modified, so the
report generation itself stays exactly as it is today.

Kill switch: set RUN_ARCHIVE=off to disable the plugin entirely (no archive,
no index rebuild, no pruning). Default is on - nothing to set up.

Archiving is reporting, not testing: every error in this plugin is logged
and swallowed, so an archive problem can never change the run's exit code.
"""
from __future__ import annotations

import contextlib
import getpass
import importlib.metadata
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Mapping
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest

from soterix_qa_shared.qatouch.mapping import canonical_nodeid
from soterix_qa_shared.reporting import live as live_record
from soterix_qa_shared.reporting.branding import favicon_link, header_logo
from soterix_qa_shared.reporting.paths import PATH_MAX, fit_name
from soterix_qa_shared.reporting.products import (
    product_for,
    product_name,
    run_product,
    target_product,
)
from soterix_qa_shared.reporting.run_report import (
    FAILURE_OUTCOMES,
    NOTES_TITLE,
    RUN_DIR_RE,
    build_run_report,
    error_line,
    exit_outcome,
    filter_script,
    friendly_stamp,
    module_of,
    one_cause,
    one_skip_reason,
    outcome_html,
    readable_title,
    summary_product,
)

log = logging.getLogger('run_archive')

RUNS_DIR = Path('reports') / 'runs'
SCREENSHOT_DIR = Path('reports') / 'screenshots'
TRACE_DIR = Path('reports') / 'artifacts'
RESULTS_JSON = Path('logs') / 'last_run_results.json'
# QA Touch case titles come from here. The file is Justin's workflow, not
# the team's, so a missing file only costs the second title line.
MAPPING_FILE = Path('config') / 'qatouch_case_mapping.json'
# run.log is written here while the session runs (the run folder does not
# exist yet) and copied into the archive at the end.
RUN_LOG = Path('logs') / 'run.log'
# The Chrome the session opened, written once by the first page fixture to
# know it (record_browser_version) and read into the provenance at the
# end. A file, because under xdist the workers open the browser and the
# controller writes the archive.
BROWSER_VERSION_FILE = Path('reports') / '.browser_version'
# Google's version history: the current stable per platform, so the
# archive can say whether a runner's Chrome is behind.
LATEST_STABLE_URL = ('https://versionhistory.googleapis.com/v1/chrome/platforms/'
                     '{platform}/channels/stable/versions')


def configure(root):
    """Root every archive path at THIS repo, from a repo root.

    The plugin calls this from pytest_sessionstart with config.rootpath; the
    health CLI calls it for the root it resolves. The module-level defaults
    above are CWD-relative, so a plugin that ran pytest from the repo root
    still worked; configure() makes the root explicit wherever it runs, and
    is the rule-5 change that lets a second consumer (C2C, Neurosen) point it
    at its own tree.
    """
    from soterix_qa_shared.reporting.layout import paths_for
    global RUNS_DIR, SCREENSHOT_DIR, TRACE_DIR, RESULTS_JSON, MAPPING_FILE, RUN_LOG, BROWSER_VERSION_FILE
    p = paths_for(root)
    BROWSER_VERSION_FILE = p['browser_version']
    RUNS_DIR = p['runs']
    SCREENSHOT_DIR = p['screenshots']
    TRACE_DIR = p['artifacts']
    RESULTS_JSON = p['run_results']
    MAPPING_FILE = p['mapping']
    RUN_LOG = p['run_log']

# 2: adds "tests" (per-test records) and "provenance".
# 3: each test record carries "error" (the failure's first error line,
#    None when it did not fail). "log_file" is set for failures only.
# 6: "utc_offset" ('+00:00', '-05:00'): the offset of the machine that ran the tests, at the
#    start. "started" and "ended" are that machine's own time with no zone, so a page could
#    not show a box run (UTC) and a laptop run (Central) on one clock.
# 4: each test record carries "product" (the name, see plugins/products)
#    and provenance carries the run's "product".
SCHEMA_VERSION = 6
# 5: adds collected / not_run / not_run_count / one_cause / one_skip_reason, and per test
# reruns, rerun_error, teardown_error, crashed when they happened.
NOT_RUN_LISTED = 500   # ids listed; not_run_count holds the true number

# Per-test log files use the screenshot sanitizer and cap: the archive's
# names then sort together, and the path cap already fits them.
LOG_NAME_MAX = 120

# The heading that separates run.log's logging records from the captured
# output of the tests that got no file of their own.
RUN_LOG_SECTION_TITLE = 'captured output of passed and skipped tests, one section per test'

# Marker tags the page objects print for things worth a human's eye that
# never fail a test on their own. Add a tag here when a page object
# starts printing a new one.
ADVISORY_TAGS = ('VIEWS', 'LPR', 'EXPORT')
_ADVISORY_RE = re.compile(r'^\[(' + '|'.join(ADVISORY_TAGS) + r')\] \S')
TERMINAL_ADVISORY_MAX = 40

# Retention is per target: a run only competes with other runs of the same
# slug, so repair workflows that rerun one file dozens of times a morning
# can never evict the full-suite archives the plugin exists to preserve.
# The total cap stops one-off targets from accumulating folders forever.
KEEP_RUNS = 20
KEEP_RUNS_TOTAL = 100
# A byte ceiling on top of the count caps. A single retain-on-failure trace
# of a livestream test reaches ~920 MB, so 20 near-duplicates of one slug
# could fill a laptop disk while staying under the count cap. Overridable
# with QA_ARCHIVE_MAX_GB. The newest run is never pruned for size.
DEFAULT_MAX_GB = 10

# Folder-name slug from the invocation. Shorter than the 120/150 name caps:
# the slug is a label, not an identifier, and every file the run produced
# nests under it - a long slug would eat the path budget of every copy.
SLUG_MAX = 40

# Copies nest the already-long trace names one folder deeper than the
# originals, which can cross Windows' 260-char MAX_PATH on a deep checkout.
# The same cap the live writes use (plugins/paths.py), so a copy shortens
# exactly the way its source would have.
DEST_PATH_MAX = PATH_MAX

# Same off-values parsing as report_media so one convention covers both.
_OFF_VALUES = ('off', '0', 'false', 'no')

# The index's Open column, one chip per link. Three distinct labels: the
# old "run report" / "report" pair read as one thing.
INDEX_LINK_RUN_REPORT = 'Run Report'
INDEX_LINK_RAW_REPORT = 'Raw pytest-html'
INDEX_LINK_FOLDER = 'Folder'

# The repo's own unit tests. A run aimed only at these produces no
# screenshots, traces, or advisories - nothing an archive folder could hold.
UNIT_TEST_ROOTS = ('tests/plugins', 'tests/tools')

# Run folders are named <session-start>__<slug> (RUN_DIR_RE, shared with
# run_report); only names with this shape are ever pruned, so index.html
# and hand-made folders can never be deleted.

# pytest's terminal convention: the call phase carries the test's outcome,
# a setup crash is an error (the test never ran), a setup skip is the
# test's skip, and a teardown failure is an error pytest counts and exits
# 1 on. Until 09/18/2026 a teardown failure counted toward nothing here,
# so the archive said 'passed' for a run pytest failed (evidence audit).
_PHASE_OUTCOME_COUNTER = {
    ('call', 'passed'): 'passed',
    ('call', 'failed'): 'failed',
    ('call', 'skipped'): 'skipped',
    ('setup', 'failed'): 'errors',
    ('setup', 'skipped'): 'skipped',
    ('teardown', 'failed'): 'errors',
}

# The same phases, in the per-test vocabulary run_report renders
# (passed / failed / skipped / error).
_PHASE_TEST_OUTCOME = {
    ('call', 'passed'): 'passed',
    ('call', 'failed'): 'failed',
    ('call', 'skipped'): 'skipped',
    ('setup', 'failed'): 'error',
    ('setup', 'skipped'): 'skipped',
}


class _RunLog:
    """The run.log handler: every INFO-or-higher record of the session.

    Attached to the root logger so the page objects' and plugins' loggers
    all reach it. The root logger's level is lowered to INFO for the
    session when it sits above (a handler cannot see what its logger
    drops) and restored on detach.
    """

    def __init__(self) -> None:
        self.handler: logging.Handler | None = None
        self.path: Path | None = None
        self._previous_level: int | None = None

    def attach(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, mode='w', encoding='utf-8')
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
        root = logging.getLogger()
        if root.level == logging.NOTSET or root.level > logging.INFO:
            self._previous_level = root.level
            root.setLevel(logging.INFO)
        root.addHandler(handler)
        self.handler, self.path = handler, path

    def detach(self) -> None:
        if self.handler is None:
            return
        root = logging.getLogger()
        root.removeHandler(self.handler)
        self.handler.close()
        self.handler = None
        if self._previous_level is not None:
            root.setLevel(self._previous_level)
            self._previous_level = None


class _RunState:
    """Facts collected across the session for the end-of-run archive."""

    def __init__(self) -> None:
        self.started: float | None = None
        self.ended: float | None = None
        self.exitstatus: int | None = None
        self.counts: dict[str, int] = {'passed': 0, 'failed': 0,
                                       'skipped': 0, 'errors': 0}
        self.advisories: list[dict] = []
        # One record per test, in execution order, keyed by node id.
        self.tests: dict[str, dict] = {}
        # The captured output of every phase, for the log/ files. Kept
        # apart from the records so summary.json stays small.
        self.captured: dict[str, list[dict]] = {}
        # Docstrings and qatouch_project markers seen at makereport, where
        # the item is in reach.
        self.docs: dict[str, str] = {}
        self.projects: dict[str, str | None] = {}
        self.mapping: dict[str, dict] | None = None
        self.mapping_project: str | None = None
        self.mapping_project_read = False
        self.run_log = _RunLog()
        # The live record of a run in progress (reporting/live.py): what
        # was collected, what is running now, where the record is written,
        # and when it was last written (writes are throttled).
        self.collected: list[str] = []
        self.current: str | None = None
        self.workers: int = 1
        self.live_paths: list[Path] = []
        self.live_written: float = 0.0
        self.config: Any = None   # pytest's config, kept for the live writes logreport makes

    @property
    def tests_run(self) -> int:
        return sum(self.counts.values())

    def case_mapping(self) -> dict[str, dict]:
        """The QA Touch mapping, read once per session."""
        if self.mapping is None:
            self.mapping = load_case_mapping(MAPPING_FILE)
        return self.mapping

    def project_key(self) -> str | None:
        """The mapping file's top-level project_key, read once per session."""
        if not self.mapping_project_read:
            self.mapping_project = load_project_key(MAPPING_FILE)
            self.mapping_project_read = True
        return self.mapping_project


_state = _RunState()


def _archive_disabled(environ: Mapping[str, str]) -> bool:
    """Kill switch: RUN_ARCHIVE=off turns the whole plugin off."""
    return environ.get('RUN_ARCHIVE', '').strip().lower() in _OFF_VALUES


def _process_may_archive(config: Any, environ: Mapping[str, str]) -> bool:
    """The guards known at session start: is this a process that archives?

    - --collect-only never archives: run_suite's planning pass collects
      before the real run, and its empty "run" would bury the real one.
    - xdist workers never archive: each worker starts its own session; the
      controller sees every result and archives once.
    - Every target is a unit-test root (tests/plugins, tests/tools): those
      runs leave no UI evidence, and a folder per plugin test run would
      bury the suite runs the archive exists for.
    """
    if config.getoption('--collect-only', default=False):
        return False
    if environ.get('PYTEST_XDIST_WORKER'):
        return False
    return not _unit_tests_only(getattr(config, 'args', None) or [])


def _should_archive(config: Any, environ: Mapping[str, str], tests_run: int) -> bool:
    """Decide whether this process may archive the finished run.

    The session-start guards above, plus: zero tests ran means nothing
    was produced, so there is nothing to keep, and a folder per typo'd
    invocation would drown the real runs.
    """
    return _process_may_archive(config, environ) and tests_run > 0


def _unit_tests_only(args: list[str]) -> bool:
    """True when every invocation target sits under a unit-test root.

    A target is a path or a node id (path::Class::test); only the path
    part counts. Separators are normalized so a backslash or an absolute
    path still lands under its root. No targets means the whole suite.
    """
    targets = [str(a) for a in args if str(a).strip()]
    if not targets:
        return False
    for target in targets:
        path = target.split('::', 1)[0].replace('\\', '/').strip('/')
        if not any(f'/{root}/' in f'/{path}/' for root in UNIT_TEST_ROOTS):
            return False
    return True


def _classify(when: str, outcome: str) -> str | None:
    """Map a report phase + outcome to a summary counter, or None to ignore.

    A failed report whose phase is not one of pytest's three is one the
    controller made up for a test it could not finish: xdist's 'worker
    crashed while running' report carries when='???'. pytest counts it as
    a failure; until 09/18/2026 this archive counted it as nothing.
    """
    counter = _PHASE_OUTCOME_COUNTER.get((when, outcome))
    if counter is None and outcome == 'failed' and when not in ('setup', 'call', 'teardown'):
        return 'failed'
    return counter


def _build_slug(args: list[str], markexpr: str = '') -> str:
    """Build the folder-name slug from what the run was asked to do.

    config.args joined, plus the -m expression when one was given - same
    sanitizer as report_media._safe_name so run folders and the files inside
    them follow one naming convention. Empty invocations become 'all'.
    """
    parts = [*list(args or []), markexpr] if markexpr else list(args or [])
    raw = ' '.join(parts)
    return re.sub(r'[^\w.-]+', '-', raw).strip('-')[:SLUG_MAX] or 'all'


def _unique_dir(parent: Path, name: str) -> Path:
    """Pick a folder name that does not collide with an existing run.

    Two quick debugging runs of the same target can start in the same
    second; the second gets a -2 (then -3, ...) suffix instead of pouring
    its copies into the first run's folder.
    """
    candidate = parent / name
    suffix = 2
    while candidate.exists():
        candidate = parent / f'{name}-{suffix}'
        suffix += 1
    return candidate


def _fit_dest(dest: Path, limit: int = DEST_PATH_MAX) -> Path | None:
    """Keep the absolute destination path under the Windows path cap.

    Returns the path unchanged when it fits, or a shortened variant when
    the filename can absorb the cut. A plain cut is not enough: nodeid-
    derived names often differ only past it (parametrized camera ids in
    a suite that runs the same test per camera), and two sources fitted
    to one name would make the later copy silently overwrite the earlier.
    fit_name (plugins/paths.py) keeps the stem's head and tail around a
    digest of the full stem, so distinct sources stay distinct. None when
    the directory leaves no room for a usable filename.
    """
    absolute = Path(os.path.abspath(dest))
    if len(str(absolute)) <= limit:
        return dest
    fitted = absolute.parent / fit_name(absolute.parent, absolute.stem, absolute.suffix, limit)
    if len(str(fitted)) > limit:
        return None
    log.warning(f'run_archive: shortened {dest.name} to {fitted.name} '
                f'to fit the {limit}-char path cap')
    return fitted


def _copy_report(config: Any, started: float, dest: Path) -> str | None:
    """Copy this run's HTML report; return the archived name, or None.

    The report path is configurable (--html): the team default is
    reports/report.html but debugging runs override it, so the option is
    the source of truth. A report older than the session start is a
    leftover from an earlier run (this run may have died before pytest-html
    wrote one) and must not be filed under this run - mtime is the
    freshness signal.
    """
    htmlpath = config.getoption('htmlpath', default=None)
    if not htmlpath:
        return None
    src = Path(htmlpath)
    try:
        if not src.is_file() or src.stat().st_mtime < started:
            return None
        shutil.copy2(src, dest / 'report.html')
    except OSError as e:
        log.warning(f'run_archive: could not copy the report {src}: {e}')
        return None
    return 'report.html'


def _copy_screenshots(started: float, dest: Path) -> None:
    """Copy this run's *.png files sitting directly in reports/screenshots/.

    report_media empties that folder at session start - but only when that
    plugin is on, so under REPORT_SCREENSHOTS=off an earlier run's images
    are still sitting there. As with the report copy, mtime against the
    session start is the freshness signal that keeps them out.
    """
    if not SCREENSHOT_DIR.is_dir():
        return
    target_root = dest / 'screenshots'
    for png in sorted(SCREENSHOT_DIR.glob('*.png')):
        if not png.is_file():
            continue
        try:
            if png.stat().st_mtime < started:
                continue
            target = _fit_dest(target_root / png.name)
            if target is None:
                log.warning(f'run_archive: skipped {png.name}: its destination '
                            f'directory leaves no room for a filename under '
                            f'the {DEST_PATH_MAX}-char path cap')
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(png, target)
        except OSError as e:
            log.warning(f'run_archive: could not copy {png}: {e}')


def _copy_traces(dest: Path) -> None:
    """Copy the full contents of reports/artifacts/ into traces/.

    That folder holds conftest's failure trace zips plus pytest-playwright's
    per-test artifact dirs, and is wiped at each session start - so at
    session end its contents belong to exactly this run.
    """
    if not TRACE_DIR.is_dir():
        return
    target_root = dest / 'traces'
    for src in sorted(TRACE_DIR.rglob('*')):
        if not src.is_file():
            continue
        try:
            target = _fit_dest(target_root / src.relative_to(TRACE_DIR))
            if target is None:
                log.warning(f'run_archive: skipped {src.name}: its destination '
                            f'directory leaves no room for a filename under '
                            f'the {DEST_PATH_MAX}-char path cap')
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
        except OSError as e:
            log.warning(f'run_archive: could not copy {src}: {e}')


def _copy_results_json(started: float, dest: Path) -> None:
    """Copy qatouch_reporter's per-nodeid outcomes JSON, when it is fresh.

    The reporter only writes the file when it is active, so a stale mtime
    means the file on disk belongs to an earlier run and stays out.
    """
    try:
        if not RESULTS_JSON.is_file() or RESULTS_JSON.stat().st_mtime < started:
            return
        shutil.copy2(RESULTS_JSON, dest / RESULTS_JSON.name)
    except OSError as e:
        log.warning(f'run_archive: could not copy {RESULTS_JSON}: {e}')


def scan_advisories(text: str | None) -> list[str]:
    """The advisory marker lines in a block of captured output, in order.

    Only lines opening with one of ADVISORY_TAGS count; the [AUTH] and
    DEBUG chatter every test prints is not an advisory. Duplicates within
    one block collapse - a poll that prints the same warning each tick
    is one advisory, not forty.
    """
    seen: set[str] = set()
    lines = []
    for raw in (text or '').splitlines():
        line = raw.strip()
        if _ADVISORY_RE.match(line) and line not in seen:
            seen.add(line)
            lines.append(line)
    return lines


def _format_advisories(advisories: list[dict]) -> str:
    """advisories.txt body: one block per test, its lines indented."""
    out = []
    current = None
    for item in advisories:
        if item.get('test') != current:
            current = item.get('test')
            out.append(str(current))
        out.append(f"  {item.get('line', '')}")
    return '\n'.join(out) + '\n'


# ---------------------------------------------------------------- per-test facts

def load_case_mapping(path: Path | None = None) -> dict[str, dict]:
    """Node id -> {'case_key', 'case_title'} from the QA Touch mapping file.

    Empty when the file is missing or not the expected shape: the case
    title is a convenience line on the page, never a reason to fail.
    """
    path = MAPPING_FILE if path is None else path
    try:
        loaded = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    mappings = loaded.get('mappings') if isinstance(loaded, dict) else None
    if not isinstance(mappings, dict):
        return {}
    out: dict[str, dict] = {}
    for nodeid, row in mappings.items():
        if isinstance(row, dict) and row.get('case_key'):
            out[str(nodeid)] = {'case_key': str(row['case_key']),
                                'case_title': str(row.get('case_title') or '')}
    return out


def load_project_key(path: Path | None = None) -> str | None:
    """The mapping file's top-level project_key, or None when the file is
    missing, unreadable, or has no such key. The products module then
    falls through to the environment and its default."""
    path = MAPPING_FILE if path is None else path
    try:
        loaded = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    key = loaded.get('project_key') if isinstance(loaded, dict) else None
    return str(key) if key else None


def lookup_case(mapping: Mapping[str, dict], nodeid: str) -> dict:
    """The mapped QA Touch case for a node id, or None fields when unmapped.

    Exact id first. Then the id without its parameter part against the
    keys without theirs: a mapping keyed on the bare function still
    names the [chromium] variant pytest-playwright runs.
    """
    nodeid = canonical_nodeid(nodeid)   # without xdist's '@<group>', which no mapping key has
    row = mapping.get(nodeid)
    if row is None:
        bare = re.sub(r'\[.*\]$', '', nodeid)
        for key, candidate in mapping.items():
            if re.sub(r'\[.*\]$', '', key) == bare:
                row = candidate
                break
    if row is None:
        return {'case_key': None, 'case_title': None}
    return {'case_key': row.get('case_key'), 'case_title': row.get('case_title') or None}


def log_file_name(nodeid: str) -> str:
    """The per-test log file's name: the sanitized node id plus .log."""
    return re.sub(r'[^\w.-]+', '-', nodeid).strip('-')[:LOG_NAME_MAX] + '.log'


def phase_capture(report: Any, stream: str) -> str:
    """The text pytest captured on `stream` (stdout or stderr) during this
    phase alone.

    A report's capstdout joins every section the item has collected so
    far, so the call report repeats setup's output and teardown repeats
    both (seen in the archived logs). The phase's own section, titled
    'Captured stdout call', holds only its text. Reports without sections
    (fakes, other plugins' objects) fall back to the joined attribute.
    """
    sections = getattr(report, 'sections', None)
    if isinstance(sections, list):
        wanted = f'Captured {stream} {report.when}'
        return ''.join(str(text) for title, text in sections if title == wanted)
    return getattr(report, f'cap{stream}', '') or ''


def record_phase(state: _RunState, report: Any, doc: str | None = None,
                 mapping: Mapping[str, dict] | None = None,
                 marker_project: str | None = None,
                 project_key: str | None = None) -> None:
    """Fold one phase report into the test's record and its captured output.

    The first phase seen creates the record (title, module, case,
    product). `marker_project` is the test's qatouch_project marker value
    and `project_key` the mapping file's default, both optional. Rerun
    outcomes (pytest-rerunfailures) are not final and are ignored; the
    attempt that ends the test overwrites the phases of the earlier ones.
    """
    nodeid = str(report.nodeid)
    rec = state.tests.get(nodeid)
    if rec is None:
        case = lookup_case(mapping or {}, nodeid)
        rec = state.tests[nodeid] = {
            'nodeid': nodeid, 'module': module_of(nodeid),
            'title': readable_title(nodeid, doc),
            'case_key': case['case_key'], 'case_title': case['case_title'],
            'product': product_name(product_for(nodeid, marker_project, project_key)),
            'outcome': None, 'duration_s': 0.0, 'phases': {}, 'log_file': None,
            'error': None, 'reason': None,
        }
    if report.outcome == 'rerun':
        # Not final, but not nothing: a test that passed on its second
        # attempt failed once, and the record says so.
        rec['reruns'] = int(rec.get('reruns') or 0) + 1
        rec.setdefault('rerun_error', error_line(getattr(report, 'longreprtext', '') or ''))
        return
    duration = float(getattr(report, 'duration', 0.0) or 0.0)
    rec['phases'][report.when] = round(duration, 3)
    rec['duration_s'] = round(sum(rec['phases'].values()), 3)
    outcome = _PHASE_TEST_OUTCOME.get((report.when, report.outcome))
    if hasattr(report, 'wasxfail') and report.when in ('setup', 'call'):
        # pytest reports an expected failure as 'skipped' and an unexpected
        # pass as 'passed'; the page has words for both
        outcome = {'skipped': 'xfailed', 'passed': 'xpassed'}.get(report.outcome, outcome)
    elif outcome is None and report.outcome == 'failed':
        if report.when == 'teardown':
            # a failed or errored test keeps its own outcome; a pass or a skip
            # whose teardown failed is an error, as pytest counts it
            outcome = 'error' if rec['outcome'] in (None, 'passed', 'skipped', 'xfailed', 'xpassed') else None
            rec['teardown_error'] = error_line(getattr(report, 'longreprtext', '') or '')
        elif report.when not in ('setup', 'call'):
            outcome = 'failed'       # the controller's stand-in for a test its worker died on
            rec['crashed'] = True
    if outcome is not None:
        rec['outcome'] = outcome
    if outcome in FAILURE_OUTCOMES:
        # The line run_report groups failures by and checks against the
        # previous runs. Kept here so no later reader needs report.html.
        rec['error'] = error_line(getattr(report, 'longreprtext', '') or '')
    if outcome == 'xfailed':
        rec['reason'] = str(getattr(report, 'wasxfail', '') or '')
    if outcome == 'skipped':
        # A skip's reason is a measured fact now (the box `without` map, the
        # stream guard, require_items); until 09/11/2026 the record kept none
        # of it, and a night's 111 skips could not be read back.
        rec['reason'] = skip_reason(report)
    state.captured.setdefault(nodeid, []).append({
        'phase': report.when, 'outcome': report.outcome, 'duration': duration,
        'stdout': phase_capture(report, 'stdout'),
        'stderr': phase_capture(report, 'stderr'),
        'error': (getattr(report, 'longreprtext', '') or '') if report.outcome == 'failed' else '',
        # a skip's reason is the phase's whole story - keep it beside the output
        'skip': (getattr(report, 'longreprtext', '') or '') if report.outcome == 'skipped' else '',
    })


def skip_reason(report: Any) -> str:
    """The reason a phase report skipped, as one line.

    pytest stores a skip as the tuple (path, line, 'Skipped: reason') in
    longrepr; longreprtext is that tuple's repr, so the message is taken
    from the tuple when there is one and from the text otherwise.
    """
    longrepr = getattr(report, 'longrepr', None)
    if isinstance(longrepr, (tuple, list)) and len(longrepr) == 3:
        text = str(longrepr[2])
    else:
        text = getattr(report, 'longreprtext', '') or str(longrepr or '')
    text = ' '.join(text.split())
    text = text.removeprefix('Skipped: ')
    return text[:400]


def format_test_log(rec: Mapping[str, Any], phases: list[dict]) -> str:
    """One test's log file: its names, then each phase's captured output."""
    lines = [str(rec.get('nodeid', '')), str(rec.get('title', ''))]
    if rec.get('case_key'):
        lines.append(f"QA Touch {rec['case_key']}: {rec.get('case_title') or ''}".rstrip(': '))
    for phase in phases:
        lines.append('')
        lines.append(f"=== {phase.get('phase')}: {phase.get('outcome')} "
                     f"({float(phase.get('duration') or 0.0):.2f} s) ===")
        wrote = False
        for label in ('stdout', 'stderr', 'error', 'skip'):
            text = str(phase.get(label) or '')
            if text.strip():
                lines.append(f'--- {label} ---')
                lines.append(text.rstrip())
                wrote = True
        if not wrote:
            lines.append('(no output)')
    return '\n'.join(lines) + '\n'


def wants_log_file(outcome: str | None) -> bool:
    """Only a failed or errored test gets its own log file. Its output is
    what a triage reads. A file per passing test buried those."""
    return outcome in FAILURE_OUTCOMES


def format_run_log(session_text: str, sections: list[str]) -> str:
    """run.log's body: the session's logging records, then the captured
    output of the tests without a file of their own, one section each."""
    parts = []
    if session_text.strip():
        parts.append(session_text.rstrip('\n') + '\n')
    if sections:
        parts.append(f'\n===== {RUN_LOG_SECTION_TITLE} =====\n\n' + '\n'.join(sections))
    return ''.join(parts) or '(no logging records)\n'


def _write_logs(dest: Path, state: _RunState) -> None:
    """Write log/run.log, and log/<test>.log for each failed or errored test.

    The captured output of every other test goes into run.log after the
    session's logging records, so nothing is lost and the folder holds
    the failures' files instead of one file per test.
    """
    log_dir = dest / 'log'
    sections: list[str] = []
    for nodeid, rec in state.tests.items():
        text = format_test_log(rec, state.captured.get(nodeid) or [])
        if not wants_log_file(rec.get('outcome')):
            sections.append(text)
            continue
        target = _fit_dest(log_dir / log_file_name(nodeid))
        if target is None:
            log.warning(f'run_archive: skipped the log of {nodeid}: its destination '
                        f'directory leaves no room for a filename under the '
                        f'{DEST_PATH_MAX}-char path cap')
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding='utf-8', errors='replace')
            rec['log_file'] = (Path('log') / target.name).as_posix()
        except OSError as e:
            log.warning(f'run_archive: could not write the log of {nodeid}: {e}')
    session_text = ''
    run_log = state.run_log.path
    if run_log is not None and run_log.is_file():
        try:
            session_text = run_log.read_text(encoding='utf-8', errors='replace')
        except OSError as e:
            log.warning(f'run_archive: could not read {run_log}: {e}')
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / 'run.log').write_text(format_run_log(session_text, sections),
                                         encoding='utf-8', errors='replace')
    except OSError as e:
        log.warning(f'run_archive: could not write run.log: {e}')


def record_browser_version(version: str | None, channel: str | None = None) -> None:
    """Note the browser this session opened, once: the first writer wins.

    Called from the consumer's page fixture, the place the browser is
    known; the archive reads it at session end into the provenance, so
    the run says which Chrome ran it and the index can say whether that
    Chrome is behind Google's stable.
    """
    if not version:
        return
    try:
        if BROWSER_VERSION_FILE.exists():
            return
        BROWSER_VERSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        BROWSER_VERSION_FILE.write_text(f'{version}\n{channel or ""}\n', encoding='utf-8')
    except OSError as e:
        log.debug(f'run_archive: could not record the browser version: {e}')


def _read_browser_version() -> str | None:
    try:
        lines = BROWSER_VERSION_FILE.read_text(encoding='utf-8').splitlines()
    except OSError:
        return None
    return (lines[0].strip() or None) if lines else None


def _stable_platform() -> str:
    if sys.platform.startswith('win'):
        return 'win64'
    if sys.platform == 'darwin':
        return 'mac'
    return 'linux'


def latest_stable_browser(timeout_s: float = 4.0) -> str | None:
    """Chrome's current stable for this platform, from Google's version
    history, or None when the network or the answer is not there. Asked
    only when the run recorded a browser version, so a session that opened
    no browser never waits on it."""
    url = LATEST_STABLE_URL.format(platform=_stable_platform())
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:  # a fixed https url, never user input
            data = json.loads(resp.read().decode('utf-8'))
        versions = data.get('versions') or []
        first = versions[0].get('version') if versions and isinstance(versions[0], dict) else None
        return str(first) if first else None
    except Exception as e:  # noqa: BLE001 - the latest version is a label; any failure reads as unknown
        log.debug(f'run_archive: no latest stable browser: {e}')
        return None


def _provenance(config: Any, environ: Mapping[str, str], product: str = '') -> dict:
    """Where and with what the run happened, and which product it tested.
    Every field tolerates absence."""
    out: dict[str, Any] = {
        'env': (environ.get('ENV') or 'qa').strip().lower(),
        # The tenant within the env. Two boxes share one host, so the host
        # alone cannot tell an India run from a US run.
        'box': (environ.get('BOX') or '').strip().lower() or None,
        'base_url_host': None,
        'git_commit': None, 'playwright': None, 'pytest_playwright': None,
        'browser': None, 'browser_channel': None, 'product': product or None,
        # Which machine ran it, as whom, signed in as which account, on which
        # Chrome: the box's nightly and a laptop run land in one archive and
        # must read apart (09/17/2026).
        'host': None, 'operator': None, 'account': None,
        'browser_version': None, 'browser_latest': None,
    }
    with contextlib.suppress(Exception):
        out['host'] = socket.gethostname() or None
    with contextlib.suppress(Exception):
        out['operator'] = getpass.getuser() or None
    try:
        from config.base_config import get_credentials
        out['account'] = str((get_credentials() or {}).get('email') or '') or None
    except Exception as e:  # noqa: BLE001 - the account is a label; a consumer without the env module has none
        log.debug(f'run_archive: no account: {e}')
    out['browser_version'] = _read_browser_version()
    if out['browser_version']:
        out['browser_latest'] = latest_stable_browser()
    try:
        from config.base_config import config as env_config
        out['base_url_host'] = urlparse(str(env_config.get('base_url') or '')).netloc or None
    except Exception as e:  # noqa: BLE001 - the env module needs .env and network names; the host is a label
        log.debug(f'run_archive: no base_url host: {e}')
    try:
        result = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], capture_output=True,
                                text=True, timeout=10, check=False)
        if result.returncode == 0:
            out['git_commit'] = result.stdout.strip() or None
    except (OSError, subprocess.SubprocessError) as e:
        log.debug(f'run_archive: no git commit: {e}')
    for key, dist in (('playwright', 'playwright'), ('pytest_playwright', 'pytest-playwright')):
        with contextlib.suppress(importlib.metadata.PackageNotFoundError):
            out[key] = importlib.metadata.version(dist)
    browsers = config.getoption('browser', default=None)
    if isinstance(browsers, (list, tuple)):
        browsers = ', '.join(str(b) for b in browsers)
    out['browser'] = str(browsers) if browsers else None
    channel = config.getoption('browser_channel', default=None)
    out['browser_channel'] = str(channel) if channel else None
    return out


def _build_summary(*, started: float, ended: float, exitstatus: int | None,
                   counts: Mapping[str, int], target: list[str], markexpr: str,
                   keyword: str, report_file: str | None,
                   advisories: list[dict] | None = None,
                   tests: list[dict] | None = None,
                   provenance: Mapping[str, Any] | None = None,
                   collected: list[str] | None = None) -> dict:
    """Build the summary.json payload. Fixed shape - the index renders it.

    `collected` is what the session set out to run. A run that stopped
    early (-x, --maxfail, a run cap, a dead controller) recorded only what
    it reached and read as a small complete run; `collected` and `not_run`
    say how much of the plan never ran. `one_cause` and `one_skip_reason`
    name the error or the skip reason most of the run shares.
    """
    tests = tests or []
    ran = {str(t.get('nodeid')) for t in tests}
    not_run = [n for n in (collected or []) if n not in ran]
    return {
        'schema_version': SCHEMA_VERSION,
        'started': datetime.fromtimestamp(started).isoformat(timespec='seconds'),  # noqa: DTZ006 - local-time display of a run timestamp; tz would shift the shown time
        'ended': datetime.fromtimestamp(ended).isoformat(timespec='seconds'),  # noqa: DTZ006 - local-time display of a run timestamp; tz would shift the shown time
        'utc_offset': _utc_offset(started),
        'duration_s': round(ended - started, 1),
        'exitstatus': exitstatus,
        'counts': {key: int(counts.get(key, 0))
                   for key in ('passed', 'failed', 'skipped', 'errors')},
        'target': list(target),
        'markexpr': markexpr or '',
        'keyword': keyword or '',
        'report_file': report_file,
        'provenance': dict(provenance or {}),
        'advisories': [dict(a) for a in (advisories or [])],
        'collected': len(collected) if collected is not None else None,
        'not_run': not_run[:NOT_RUN_LISTED],
        'not_run_count': len(not_run),
        'one_cause': one_cause(tests),
        'one_skip_reason': one_skip_reason(tests),
        'tests': [dict(t, phases=dict(t.get('phases') or {})) for t in tests],
    }


def _utc_offset(epoch: float) -> str:
    """This machine's offset from UTC at that moment, as '+05:30'."""
    import time
    try:
        seconds = time.localtime(epoch).tm_gmtoff or 0
    # a time that the platform's clock cannot place (Windows refuses one near 1970) has no offset to record
    except (OSError, OverflowError, ValueError):
        return ''
    sign = '-' if seconds < 0 else '+'
    hours, minutes = divmod(abs(seconds) // 60, 60)
    return f'{sign}{hours:02d}:{minutes:02d}'


def _select_prunable(names: list[str], keep: int = KEEP_RUNS,
                     total: int = KEEP_RUNS_TOTAL) -> list[str]:
    """Pick the run-folder names that retention lets go.

    A run stays while it is among the newest `keep` of its own slug AND
    the newest `total` overall. Per-slug counting is the point: a flat
    newest-N would let a morning of debug iterations on one file evict
    the full-suite archives the plugin exists to preserve.

    Only names matching RUN_DIR_RE are candidates; their timestamp prefix
    makes name order chronological order, so a plain sort finds the oldest.
    """
    runs = sorted((n for n in names if RUN_DIR_RE.match(n)), reverse=True)
    seen_per_slug: dict[str, int] = {}
    prunable = []
    for rank, name in enumerate(runs):
        slug = name.partition('__')[2]
        seen_per_slug[slug] = seen_per_slug.get(slug, 0) + 1
        if seen_per_slug[slug] > keep or rank >= total:
            prunable.append(name)
    return prunable


def _prune_runs(runs_dir: Path, keep: int = KEEP_RUNS,
                total: int = KEEP_RUNS_TOTAL) -> None:
    """Delete the run folders retention lets go, and nothing else.

    Deliberately narrow - only directories directly inside runs_dir whose
    names match the run pattern, never anything outside it - so a
    misconfigured path can never eat more than old archives.
    """
    if not runs_dir.is_dir():
        return
    names = [p.name for p in runs_dir.iterdir() if p.is_dir()]
    for name in _select_prunable(names, keep, total):
        try:
            shutil.rmtree(runs_dir / name)
            log.info(f'run_archive: pruned old run {name}')
        except OSError as e:
            log.warning(f'run_archive: could not prune {name}: {e}')
    _prune_by_size(runs_dir)


def _dir_bytes(path: Path) -> int:
    """Total bytes under a directory. Best effort - a file that vanishes
    mid-walk (a concurrent prune) is skipped, not fatal."""
    total = 0
    for p in path.rglob('*'):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def _prune_by_size(runs_dir: Path) -> None:
    """Remove oldest runs until the archive is under the byte ceiling.

    Runs after the count-based prune. The newest run is never removed for
    size: a single run over the cap (one huge livestream trace) is kept, or
    the plugin would delete the very evidence a developer is about to open.
    """
    try:
        max_gb = float(os.environ.get('QA_ARCHIVE_MAX_GB', DEFAULT_MAX_GB))
    except (TypeError, ValueError):
        max_gb = DEFAULT_MAX_GB
    ceiling = int(max_gb * 1024 ** 3)
    runs = sorted((p for p in runs_dir.iterdir()
                   if p.is_dir() and RUN_DIR_RE.match(p.name)),
                  key=lambda p: p.name)          # oldest first
    if len(runs) <= 1:
        return
    sizes = {p: _dir_bytes(p) for p in runs}
    used = sum(sizes.values())
    newest = runs[-1]
    for p in runs:
        if used <= ceiling:
            break
        if p is newest:
            continue                             # never the newest
        try:
            shutil.rmtree(p)
            used -= sizes[p]
            log.info(f'run_archive: pruned {p.name} for size '
                     f'(archive over {max_gb} GB)')
        except OSError as e:
            log.warning(f'run_archive: could not size-prune {p.name}: {e}')


def _load_entries(runs_dir: Path) -> list[dict]:
    """Read each run folder's summary.json into an index entry.

    Scanning the folders instead of remembering state keeps the index
    correct when folders are hand-deleted or hold no readable summary
    (older layouts, an archive that died mid-write) - such folders still
    get a row built from their name alone.
    """
    entries: list[dict] = []
    if not runs_dir.is_dir():
        return entries
    for folder in sorted(runs_dir.iterdir()):
        if not folder.is_dir() or not RUN_DIR_RE.match(folder.name):
            continue
        summary: dict = {}
        try:
            loaded = json.loads((folder / 'summary.json').read_text(encoding='utf-8'))
            if isinstance(loaded, dict):
                summary = loaded
        except (OSError, ValueError):
            pass
        entries.append({'name': folder.name, 'summary': summary,
                        'has_report': (folder / 'report.html').is_file(),
                        'has_run_report': (folder / 'run.html').is_file()})
    return entries


def _render_index(entries: list[dict], has_health: bool = False,
                  default_key: str | None = None) -> str:
    """Render the archive index page, newest run first.

    Plain, self-contained HTML: the page is opened from disk, so it can
    depend on nothing but the run folders sitting next to it. Every summary
    key is optional - a row shows '-' where a value is unknown, except the
    product: a run that recorded none gets the one its targets name
    (`default_key` is the mapping file's project_key, the fallback when
    no target names one). The outcome is the exit status as a word, the
    code in its tooltip. Notes is the advisory count. The Open cell holds
    one link chip per file the folder has: Run Report (run.html), Raw
    pytest-html (report.html), and always Folder. The Health chip only
    appears when health.html exists beside the index. Each row carries
    its visible words in data-test for the filter box.
    """
    rows = []
    for entry in sorted(entries, key=lambda e: e.get('name', ''), reverse=True):
        name = entry.get('name', '')
        summary = entry.get('summary') or {}
        counts = summary.get('counts') or {}
        stamp, _, slug = name.partition('__')
        started = summary.get('started') or stamp.replace('_', ' ')
        target = ' '.join(summary.get('target') or []) or slug
        provenance = summary.get('provenance') if isinstance(summary.get('provenance'), dict) else {}
        # Two boxes share one host and one target string; the box is what
        # tells their rows apart, so it sits beside the target when recorded.
        # The machine sits beside it: the nightly's box and a laptop's run
        # land in the same archive.
        box = str(provenance.get('box') or '')
        host = str(provenance.get('host') or '')
        product = (summary_product(summary)
                   or target_product(summary.get('target') or [], default_key))
        exitstatus = summary.get('exitstatus')
        advisories = summary.get('advisories')
        advisory_count = len(advisories) if isinstance(advisories, list) else '-'
        folder = escape(name, quote=True)
        links = []
        if entry.get('has_run_report'):
            links.append(f'<a href="{folder}/run.html">{INDEX_LINK_RUN_REPORT}</a>')
        if entry.get('has_report'):
            links.append(f'<a href="{folder}/report.html">{INDEX_LINK_RAW_REPORT}</a>')
        links.append(f'<a href="{folder}/">{INDEX_LINK_FOLDER}</a>')
        cells = [
            # the readable form; the ISO stamp stays in the tooltip and the search words
            f'<span title="{escape(str(started), quote=True)}">{escape(friendly_stamp(started))}</span>',
            escape(product),
            escape(str(target)) + (f' <code class="box">{escape(box)}</code>' if box else '')
            + (f' <code class="host" title="ran on {escape(host, quote=True)}">{escape(host)}</code>' if host else ''),
            *[escape(str(counts.get(key, '-')))
              for key in ('passed', 'failed', 'skipped', 'errors')],
            outcome_html(exitstatus),
            escape(str(advisory_count)),
            '<span class="open">' + ''.join(links) + '</span>',
        ]
        words = ' '.join(str(w) for w in (started, friendly_stamp(started), product, target,
                                           box, host, provenance.get('account'),
                                           provenance.get('operator'), provenance.get('browser_version'),
                                           exit_outcome(exitstatus), name) if w)
        rows.append(f'<tr data-test="{escape(words.lower(), quote=True)}">'
                    + ''.join(f'<td>{cell}</td>' for cell in cells) + '</tr>')

    body = '\n'.join(rows) if rows else ('<tr><td colspan="10">No archived runs '
                                         'yet. Run the suite once.</td></tr>')
    runners = _runners_html(entries)
    health = ('<p><span class="open"><a href="health.html">Health</a></span> pass rates '
              'per target and module, and the tests to watch, across recent runs.</p>'
              if has_health
              else '<p><code>python run.py health</code> builds health.html: pass rates per '
                   'target and module, and the tests to watch, across recent runs.</p>')
    return f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Test Run Archive</title>
{favicon_link()}
<style>
.brand-row {{ display: flex; align-items: center; gap: 1rem; margin-bottom: 0.5rem; }}
.brand-row h1 {{ margin: 0; }}
.brand {{ display: block; }}
body {{ font-family: system-ui, sans-serif; margin: 2rem; }}
table {{ border-collapse: collapse; }}
th, td {{ border: 1px solid #ccc; padding: 0.35rem 0.7rem; text-align: left; }}
th {{ background: #f0f0f0; }}
th[title] {{ text-decoration: underline dotted; cursor: help; }}
input#filter {{ width: min(100%, 36rem); padding: 0.45rem 0.7rem; font: inherit;
  border: 1px solid #9a9a9a; border-radius: 6px; }}
#filter-count {{ color: #5f6368; margin-left: 0.75rem; font-size: 0.9rem; }}
.open {{ display: inline-flex; flex-wrap: wrap; gap: 0.4rem; vertical-align: middle; }}
.open a {{ border: 1px solid #9a9a9a; border-radius: 4px; padding: 0.1rem 0.5rem;
  font-size: 0.85rem; text-decoration: none; white-space: nowrap; }}
.open a:hover {{ background: #eef3ff; }}
code.host {{ background: #eef3ff; border-radius: 4px; padding: 0 0.3rem; }}
.behind {{ color: #b00020; }}
</style>
</head>
<body>
<div class="brand-row">{header_logo()}<h1>Test Run Archive</h1></div>
{live_record.banner_html()}
<p>One folder per test run, newest first. Open a run's <b>{INDEX_LINK_RUN_REPORT}</b>
for its failures with their screenshots, video, and trace. <b>{INDEX_LINK_RAW_REPORT}</b>
is the report file pytest wrote. The archive keeps the newest {KEEP_RUNS} runs of each
target ({KEEP_RUNS_TOTAL} in total) and rebuilds this page after every run.</p>
{health}
{runners}
<p><input id="filter" type="search" placeholder="Filter runs: product, target, machine, account, outcome, date"
 aria-label="Filter runs"><span id="filter-count"></span></p>
<table>
<tr><th>Started</th><th>Product</th><th>Target</th><th>Passed</th><th>Failed</th>
<th>Skipped</th><th>Errors</th><th>Outcome</th>
<th title="{escape(NOTES_TITLE, quote=True)}">Notes</th><th>Open</th></tr>
{body}
</table>
<script>{filter_script('runs')}</script>
</body>
</html>
'''


def _runners_html(entries: list[dict]) -> str:
    """One line naming each machine's Chrome, from its newest run that
    recorded one, and whether Google's stable is newer. The box's nightly
    and the laptops share this archive, and which Chrome ran a run is the
    first question when a livestream test turns. Empty when no run
    recorded a machine."""
    newest: dict[str, dict] = {}
    for entry in sorted(entries, key=lambda e: e.get('name', ''), reverse=True):
        summary = entry.get('summary') or {}
        prov = summary.get('provenance') if isinstance(summary.get('provenance'), dict) else {}
        host, version = prov.get('host'), prov.get('browser_version')
        if host and version and host not in newest:
            newest[str(host)] = prov
    if not newest:
        return ''
    parts = []
    for host, prov in sorted(newest.items()):
        version, latest = str(prov['browser_version']), prov.get('browser_latest')
        browser = str(prov.get('browser_channel') or prov.get('browser') or 'browser').capitalize()
        if latest and str(latest) != version:
            note = f' <span class="behind">(stable {escape(str(latest))} is out: update it)</span>'
        elif latest:
            note = ' (current stable)'
        else:
            note = ''
        parts.append(f'<code class="host">{escape(host)}</code> {escape(browser)} {escape(version)}{note}')
    return '<p class="runners">Runners, by their newest run: ' + '; '.join(parts) + '.</p>'


def _write_index(runs_dir: Path) -> None:
    """Regenerate index.html from whatever run folders exist right now."""
    if not runs_dir.is_dir():
        return
    (runs_dir / 'index.html').write_text(
        _render_index(_load_entries(runs_dir), has_health=(runs_dir / 'health.html').is_file(),
                      default_key=load_project_key(MAPPING_FILE)),
        encoding='utf-8')


_OUTCOME_COUNTER = {'passed': 'passed', 'xpassed': 'passed', 'failed': 'failed',
                    'skipped': 'skipped', 'xfailed': 'skipped', 'error': 'errors'}


def _counts_of(state: _RunState) -> dict[str, int]:
    """The summary's counts, one per TEST: the records' final outcomes.

    The live counters count reports, as pytest's terminal does, and a pass
    whose teardown failed is two reports: the verdict then read '8 of 12'
    for a run of 11. Counting the records keeps counts and records equal.
    A session with no records (the recorder failed) falls back to the
    live counters, which are better than zeros.
    """
    if not state.tests:
        return dict(state.counts)
    counts = dict.fromkeys(('passed', 'failed', 'skipped', 'errors'), 0)
    for rec in state.tests.values():
        key = _OUTCOME_COUNTER.get(str(rec.get('outcome')))
        if key:
            counts[key] += 1
    return counts


def _archive_run(config: Any, state: _RunState) -> None:
    """Copy the finished run's outputs into its own dated folder."""
    markexpr = config.getoption('markexpr', default='') or ''
    keyword = config.getoption('keyword', default='') or ''
    stamp = datetime.fromtimestamp(state.started).strftime('%Y-%m-%d_%H-%M-%S')  # noqa: DTZ006 - local-time display of a run timestamp; tz would shift the shown time
    dest = _unique_dir(RUNS_DIR, f'{stamp}__{_build_slug(config.args, markexpr)}')
    dest.mkdir(parents=True, exist_ok=True)

    report_file = _copy_report(config, state.started, dest)
    _copy_screenshots(state.started, dest)
    _copy_traces(dest)
    _copy_results_json(state.started, dest)
    # Before the summary: writing the logs fills in each record's log_file.
    _write_logs(dest, state)

    # A sessionfinish that never ran (crash before initstate 2) leaves ended
    # unset; the archive still records an honest end time.
    ended = state.ended if state.ended is not None else time.time()
    # The run's product from its tests. A run with records but no product
    # (none resolved) falls back to what a test of it would resolve to.
    product = (run_product(t.get('product') for t in state.tests.values())
               or product_name(product_for('', None, state.project_key())))
    summary = _build_summary(started=state.started, ended=ended,
                             exitstatus=state.exitstatus, counts=_counts_of(state),
                             target=config.args, markexpr=markexpr,
                             keyword=keyword, report_file=report_file,
                             advisories=state.advisories,
                             tests=list(state.tests.values()),
                             provenance=_provenance(config, os.environ, product),
                             collected=list(state.collected) if state.collected else None)
    (dest / 'summary.json').write_text(json.dumps(summary, indent=2),
                                       encoding='utf-8')
    if state.advisories:
        (dest / 'advisories.txt').write_text(_format_advisories(state.advisories),
                                             encoding='utf-8')
    _write_run_report(dest)
    log.info(f'run_archive: archived the run to {dest}')


def _write_run_report(dest: Path) -> None:
    """Build the run's own page from the copies just filed.

    Last, so it sees summary.json and every copy. The builder reads only
    the folder, so `python -m plugins.run_report <folder>` can rebuild the
    page later. A builder error costs the page, never the archive.
    """
    try:
        (dest / 'run.html').write_text(build_run_report(dest), encoding='utf-8')
    except Exception as e:  # noqa: BLE001 - the page is a convenience; the copies already stand
        log.warning(f'run_archive: could not build run.html for {dest.name}: {e}')


@pytest.hookimpl(tryfirst=True)
def pytest_sessionstart(session: Any) -> None:
    """Record the session start time - it names the archive folder, and any
    report or results file older than it belongs to an earlier run - and
    start run.log in every process that will archive. First among the
    sessionstart hooks, so the other plugins' start-up lines land in it."""
    configure(session.config.rootpath)
    _state.started = time.time()
    if _archive_disabled(os.environ) or not _process_may_archive(session.config, os.environ):
        return
    _warn_low_disk()
    # An earlier run's browser version must not label this one; the
    # archiving process clears it before any worker opens a browser.
    with contextlib.suppress(OSError):
        BROWSER_VERSION_FILE.unlink(missing_ok=True)
    try:
        _state.run_log.attach(RUN_LOG)
    except Exception as e:  # noqa: BLE001 - a lost run.log must never block the session
        log.warning(f'run_archive: could not start {RUN_LOG}: {e}')
    _start_live(session.config)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: Any, call: Any):
    """Remember the test's docstring and qatouch_project marker while the
    item is in reach.

    The phase itself is recorded in logreport below, which also fires on
    the xdist controller (the process that archives) where this hook
    does not.
    """
    yield
    if item.nodeid in _state.docs:
        return
    try:
        doc = getattr(getattr(item, 'obj', None), '__doc__', None)
        _state.docs[item.nodeid] = doc if isinstance(doc, str) else ''
        _state.projects[item.nodeid] = marker_project(item)
    except Exception as e:  # noqa: BLE001 - a title fallback is cheaper than a broken run
        log.debug(f'run_archive: no docstring for {item.nodeid}: {e}')


def marker_project(item: Any) -> str | None:
    """The value of the test's qatouch_project marker, or None without one."""
    get_marker = getattr(item, 'get_closest_marker', None)
    marker = get_marker('qatouch_project') if callable(get_marker) else None
    args = getattr(marker, 'args', None) if marker is not None else None
    return str(args[0]) if args else None


def pytest_runtest_logreport(report: Any) -> None:
    """Tally outcomes for summary.json and the zero-test guard, keep the
    advisory marker lines each phase printed, and record the phase's
    facts and captured output for the test's summary record and log."""
    counter = _classify(report.when, report.outcome)
    if counter is not None:
        _state.counts[counter] += 1
    if report.when in ('setup', 'call'):
        # The phase's own capture: capstdout would repeat setup's lines
        # under the call phase and file each advisory twice.
        captured = phase_capture(report, 'stdout') + '\n' + phase_capture(report, 'stderr')
        for line in scan_advisories(captured):
            _state.advisories.append({'test': report.nodeid,
                                      'phase': report.when, 'line': line})
    try:
        record_phase(_state, report, doc=_state.docs.get(report.nodeid),
                     mapping=_state.case_mapping(),
                     marker_project=_state.projects.get(report.nodeid),
                     project_key=_state.project_key())
    except Exception as e:  # noqa: BLE001 - per-test facts are reporting, never a verdict
        log.debug(f'run_archive: could not record {report.nodeid}: {e}')
    if _state.live_paths and _state.config is not None:
        _write_live(_state.config, _state)


def _live_enabled(config: Any) -> bool:
    """The live record follows the archive: same kill switch, same process."""
    return not _archive_disabled(os.environ) and _process_may_archive(config, os.environ)


def _live_record(config: Any, state: _RunState, now: float) -> dict:
    """The record for this moment, from the session's facts (reporting/live.py)."""
    target = list(config.args)
    default_key = state.project_key()
    product = product_name(target_product(target, default_key)) if target else product_name(product_for('', None, default_key))
    remaining = [n for n in state.collected if n not in state.tests]
    return live_record.build_record(
        started=state.started or now, now=now, target=target,
        box=(os.environ.get('BOX') or '').strip().lower() or None,
        env=(os.environ.get('ENV') or 'qa').strip().lower(), product=product,
        workers=state.workers, collected=len(state.collected), counts=state.counts,
        current=state.current, remaining=remaining, runs_dir=RUNS_DIR,
        hostname=socket.gethostname())


def _write_live(config: Any, state: _RunState, force: bool = False) -> None:
    """Refresh live.json, at most every MIN_WRITE_GAP_S unless forced."""
    now = time.time()
    if not state.live_paths or (not force and now - state.live_written < live_record.MIN_WRITE_GAP_S):
        return
    try:
        live_record.write_live(state.live_paths, _live_record(config, state, now))
        state.live_written = now
    except Exception as e:  # noqa: BLE001 - the live record is a courtesy, never a verdict
        log.debug(f'run_archive: could not write the live record: {e}')


def _start_live(config: Any, collected: list[str] | None = None) -> None:
    """Open the live record for this session (idempotent) and write it.

    Called at session start, so the archive pages show the run from its
    first second, and again when the collected ids are known. Under xdist
    the controller never collects (xdist's DSession returns True from
    pytest_collection, which is where pytest would call collection_finish),
    so the ids arrive from the first worker's collection instead; a session
    that started the record but never learned the ids stays at
    "collecting", never blank.
    """
    if not _state.live_paths:
        try:
            workers = config.getoption('numprocesses', None)
            _state.workers = int(workers) if workers and str(workers).isdigit() else 1
        except Exception:  # noqa: BLE001 - why: no xdist means one worker, and the option is not there to read
            _state.workers = 1
        _state.config = config
        _state.live_paths = live_record.live_paths(RUNS_DIR, os.environ)
    if collected is not None and not _state.collected:
        _state.collected = list(collected)
    _write_live(config, _state, force=True)


def pytest_collection_finish(session: Any) -> None:
    """In-process runs: the collected node ids come from the session itself."""
    if not _live_enabled(session.config):
        return
    _start_live(session.config, [item.nodeid for item in session.items])


@pytest.hookimpl(optionalhook=True)
def pytest_xdist_node_collection_finished(node: Any, ids: Any) -> None:
    """xdist controller: the first worker to finish collecting names the
    run's tests. Every worker collects the same ids, so the rest are
    ignored. optionalhook: the API repo has no xdist, and pytest refuses a
    plugin that implements a hook nobody declared."""
    if not _state.live_paths or _state.collected:
        return
    _start_live(_state.config, [str(i) for i in ids])


def pytest_runtest_logstart(nodeid: str, location: Any) -> None:
    """The test running now, for the banner. Fires on the controller under
    xdist as well, forwarded from the worker that started it."""
    _state.current = nodeid


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    """Record when the session ended and how it exited."""
    _state.ended = time.time()
    _state.exitstatus = int(exitstatus)


def _print_one_cause(terminalreporter: Any) -> None:
    """One line when most of the run failed, or skipped, for one reason: a
    tenant that is down or a browser that is missing otherwise reads as
    hundreds of unrelated failures (failure-mode audit, 09/18/2026)."""
    tests = list(_state.tests.values())
    cause, skip = one_cause(tests), one_skip_reason(tests)
    not_run = [n for n in _state.collected if n not in _state.tests]
    if not (cause or skip or not_run):
        return
    terminalreporter.section('read this first')
    if cause:
        terminalreporter.write_line(
            f"ONE CAUSE, {cause['count']} of {cause['of']} failures: {cause['line']} "
            f"(first: {cause['first']}). Fix this before reading the rest.")
    if skip:
        terminalreporter.write_line(f"{skip['count']} tests skipped for one reason: {skip['reason']}")
    if not_run:
        terminalreporter.write_line(
            f'{len(not_run)} of {len(_state.collected)} collected tests did not run '
            f'(first: {not_run[0]}). The counts below cover only what ran.')


def pytest_terminal_summary(terminalreporter: Any, exitstatus: int, config: Any) -> None:
    """Print the run's advisory lines where the team will actually see them.

    Passing tests swallow their output, so the [VIEWS]/[LPR]/[EXPORT]
    markers would otherwise only exist inside the archive.
    """
    try:
        _print_one_cause(terminalreporter)
    except Exception as e:  # noqa: BLE001 - a summary must never fail the run
        log.debug(f'run_archive: could not print the one-cause lines: {e}')
    advisories = _state.advisories
    if not advisories:
        return
    try:
        terminalreporter.section(f'run advisories ({len(advisories)})')
        for item in advisories[:TERMINAL_ADVISORY_MAX]:
            terminalreporter.write_line(f"{item['test']}: {item['line']}")
        if len(advisories) > TERMINAL_ADVISORY_MAX:
            terminalreporter.write_line(
                f'... {len(advisories) - TERMINAL_ADVISORY_MAX} more in the '
                f"run's advisories.txt under {RUNS_DIR}/")
    except Exception as e:  # noqa: BLE001 - a summary must never fail the run
        log.debug(f'run_archive: could not print advisories: {e}')


LOW_DISK_BYTES = 1024 ** 3   # warn under 1 GB free where the reports are written


def _archive_failure_marker() -> Path:
    """Beside run.log: the one place a wrapper (run.py, a nightly script) can
    read why the newest run has no archive folder."""
    return RUN_LOG.parent / 'archive_failed.txt'


def _free_mb(path: Path) -> int | None:
    try:
        probe = path if path.exists() else path.parent
        return int(shutil.disk_usage(probe).free / (1024 * 1024))
    except OSError:
        return None


def _say_archive_failed(error: BaseException) -> None:
    """A run whose evidence could not be written said nothing on the terminal:
    the warning went to a log file, and the wrapper then guessed 'no test ran'
    (failure-mode audit, 09/18/2026). Say it where the reader is, and leave
    the reason in a marker file."""
    free = _free_mb(RUNS_DIR)
    line = (f'run_archive: THIS RUN WAS NOT ARCHIVED: {type(error).__name__}: {error} '
            f'(free space where reports are written: {free if free is not None else "unknown"} MB)')
    print('\n' + line, file=sys.stderr, flush=True)
    try:
        marker = _archive_failure_marker()
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(line + '\n', encoding='utf-8')
    except OSError as e:
        log.debug(f'run_archive: could not write the archive-failure marker: {e}')


def _clear_archive_failure() -> None:
    with contextlib.suppress(OSError):
        _archive_failure_marker().unlink(missing_ok=True)


def _warn_low_disk() -> None:
    free = _free_mb(RUNS_DIR)
    if free is not None and free * 1024 * 1024 < LOW_DISK_BYTES:
        print(f'run_archive: only {free} MB free where reports are written ({RUNS_DIR}); '
              f'this run may not be archived. Free space before a long run.', file=sys.stderr, flush=True)


def pytest_unconfigure(config: Any) -> None:
    """Archive the finished run, prune old runs, rebuild the index.

    This hook - not sessionfinish - because pytest fires unconfigure
    strictly after every sessionfinish (wrap_session in _pytest/main.py
    runs the sessionfinish hook, then _ensure_unconfigure). pytest-html
    writes the report file during ITS sessionfinish and conftest mails it
    during its own, so by unconfigure the report is complete and sent and
    the copies here always see the finished file.
    """
    # Closed first so the archive copies a complete, flushed run.log.
    _state.run_log.detach()
    # The session is over whatever happens next: the live record comes
    # down here, before any early return, so the banner never outlives
    # the run it announced.
    live_record.clear_live(_state.live_paths)
    if _archive_disabled(os.environ):
        return
    if not _should_archive(config, os.environ, _state.tests_run):
        return
    try:
        _archive_run(config, _state)
        _clear_archive_failure()
    except Exception as e:  # noqa: BLE001 - archiving must never change the run's exit code
        log.warning(f'run_archive: could not archive the run: {e}')
        _say_archive_failed(e)
    try:
        _prune_runs(RUNS_DIR)
    except Exception as e:  # noqa: BLE001 - pruning is best-effort housekeeping
        log.warning(f'run_archive: could not prune old runs: {e}')
    try:
        _write_index(RUNS_DIR)
    except Exception as e:  # noqa: BLE001 - a stale index is better than a broken run
        log.warning(f'run_archive: could not rebuild the index: {e}')
