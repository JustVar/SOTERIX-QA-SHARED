"""QA Touch Result Reporter -- pytest plugin.

Records what a test session produced and hands it to every registered sink.

Activation for the QA Touch upload:
  pytest --qatouch-upload --qatouch-run <RUN_KEY>
  or:  QATOUCH_UPLOAD=1  QATOUCH_RUN_KEY=<key>  pytest ...

The local run record is written on EVERY session, armed or not. It costs
nothing, and the tools that read it - the flaky history, the run
cross-check, the archive - used to get nothing at all from an ordinary
`pytest` run, because before the sink split the plugin only existed when an
upload was armed.

Structure:
  * this module      - pytest hooks, node-id records, sink fan-out
  * `results`        - `RunResult`, the `ResultSink` interface, the local
                       run-record sink
  * `upload`         - the QA Touch upload sink and everything about that API
  * `mapping`        - pure node-id/case-key helpers
  * `artifacts`      - the frozen shapes of the JSON other tools parse

Node id is the identity throughout. See `results` for why a case key is not.

Two things this module deliberately does NOT own, because it lives in
site-packages rather than in a test repo:

  * **Paths.** Every writer path derives from a root the consumer supplies
    through `configure(root)`. `__file__` points into the installed package,
    so it can no longer stand in for the repo root.
  * **Mapping policy.** Which mapping rows may upload is a per-repo decision
    (the UI gates on a `reviewed` flag; the API does not). The consumer's
    shim assigns `MAPPING_POLICY` before the module alias is installed. With
    no policy the reporter resolves no cases and says so - it never guesses
    a permissive default, because an unreviewed guess writes a real status
    onto a case no test verifies.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import pytest

from soterix_qa_shared.qatouch.layout import paths_for
from soterix_qa_shared.qatouch.mapping import base_keyed
from soterix_qa_shared.qatouch.results import (
    ResultSink,
    RunResult,
    RunResultsFileSink,
    merge_reruns,
    worse_outcome,
)
from soterix_qa_shared.qatouch.upload import (
    DEFAULT_PROJECT_KEY,
    MAX_ATTACHMENT_BYTES,
    MAX_RETRIES,
    PYTEST_TO_QT,
    RETRY_BASE_DELAY,
    STATUS_BLOCKED,
    STATUS_FAILED,
    STATUS_PASSED,
    QATouchUploadSink,
    format_duration,
)

log = logging.getLogger('qatouch_reporter')

# Re-exported for the consumer shims: these names were importable from the
# repo-local module this package replaces, and tools still read them.
__all__ = [
    'DEFAULT_PROJECT_KEY',
    'MAX_ATTACHMENT_BYTES',
    'MAX_RETRIES',
    'PYTEST_TO_QT',
    'RETRY_BASE_DELAY',
    'STATUS_BLOCKED',
    'STATUS_FAILED',
    'STATUS_PASSED',
    'QATouchReporter',
    'QATouchUploadSink',
    'ResultSink',
    'RunResult',
    'RunResultsFileSink',
    'configure',
    'format_duration',
    'pytest_addoption',
    'pytest_configure',
    'worse_outcome',
]

# ---------------------------------------------------------------------------
# Consumer-supplied state
#
# These are module globals rather than computed-on-use values on purpose:
# the reporter's test suite monkeypatches them, and a recompute-on-use
# variant silently ignored those patches and wrote into the real repo.
# Every reader below looks them up at CALL time, never captures them at
# import time or in __init__.
# ---------------------------------------------------------------------------

ROOT: Path | None = None
MAPPING_PATH: Path | None = None
FAILURE_LOG_DIR: Path | None = None
FAILURE_LOG_PATH: Path | None = None
RUN_RESULTS_PATH: Path | None = None

# policy(path: Path) -> (mapping, counts). Set by the consumer's shim before
# the module alias is installed. See the module docstring.
MAPPING_POLICY: Any = None


def configure(root: Path | str) -> None:
    """Point every writer path at `root`.

    Called from `pytest_configure` with `config.rootpath`, and by any CLI
    that resolves the repo root itself. Safe to call more than once; the
    last call wins.
    """
    global ROOT, MAPPING_PATH, FAILURE_LOG_DIR, FAILURE_LOG_PATH, RUN_RESULTS_PATH
    paths = paths_for(root)
    ROOT = paths['root']
    MAPPING_PATH = paths['mapping']
    FAILURE_LOG_DIR = paths['logs']
    FAILURE_LOG_PATH = paths['failure_log']
    RUN_RESULTS_PATH = paths['run_results']


def pytest_addoption(parser: Any) -> None:
    group = parser.getgroup('qatouch', 'QA Touch result reporting')
    group.addoption(
        '--qatouch-upload',
        action='store_true',
        default=False,
        help='Upload test results to QA Touch after the session.',
    )
    group.addoption(
        '--qatouch-run',
        default=None,
        help='QA Touch test run key (e.g. "d6pw"). Also: QATOUCH_RUN_KEY env var.',
    )
    group.addoption(
        '--no-run-record',
        action='store_true',
        default=False,
        help='Skip writing logs/last_run_results.json for this session.',
    )


def _tests(n: int) -> str:
    return 'test' if n == 1 else 'tests'


def _env_flag(name: str, default: str = '') -> bool:
    return os.environ.get(name, default).lower() in ('1', 'true', 'yes')


def _build_sinks(config: Any) -> list[ResultSink]:
    """Decide where this session's results go.

    The local run record is unconditional; the QA Touch upload waits to be
    armed. Keeping them separate is the point of the split - a run that does
    not upload still leaves the record its own repo's tools read.
    """
    sinks: list[ResultSink] = []

    upload = (config.getoption('--qatouch-upload', default=False)
              or _env_flag('QATOUCH_UPLOAD'))
    run_key = (config.getoption('--qatouch-run', default=None)
               or os.environ.get('QATOUCH_RUN_KEY'))
    project_key = os.environ.get('QATOUCH_PROJECT_ID', DEFAULT_PROJECT_KEY)

    upload_sink = None
    if upload and not run_key:
        log.warning('QA Touch upload enabled but no test run key provided. '
                    'Use --qatouch-run <key> or QATOUCH_RUN_KEY env var.')
    elif upload:
        use_playwright = _env_flag('QATOUCH_USE_PLAYWRIGHT_ENDPOINT')
        # Failure-screenshot capture is on by default for the playwright
        # endpoint, since legacy uploads ignore attachments anyway.
        capture = use_playwright and os.environ.get(
            'QATOUCH_CAPTURE_SCREENSHOTS', '1').lower() not in ('0', 'false', 'no')
        upload_sink = QATouchUploadSink(
            run_key=run_key,
            project_key=project_key,
            use_playwright_endpoint=use_playwright,
            capture_screenshots=capture,
            failure_log_path=FAILURE_LOG_PATH,
        )

    if not config.getoption('--no-run-record', default=False):
        sinks.append(RunResultsFileSink(
            path=RUN_RESULTS_PATH,
            run_key=run_key if upload_sink else None,
            project_key=project_key,
        ))
    if upload_sink:
        sinks.append(upload_sink)
    return sinks


def pytest_configure(config: Any) -> None:
    # Paths first, before anything decides whether to report: the sinks are
    # built from them. Guarded because an exception raised here aborts the
    # whole session with INTERNALERROR - seen for real when a Windows
    # 8.3-vs-long path made rootdir fall back to C:\Users.
    try:
        configure(config.rootpath)
    except Exception as exc:
        print(f'QA Touch: could not resolve the repo root ({exc}). '
              f'Result reporting is off for this session.')
        return

    try:
        sinks = _build_sinks(config)
        if not sinks:
            return
        reporter = QATouchReporter(sinks)
    except Exception as exc:
        print(f'QA Touch: the reporter could not start ({exc}). '
              f'No results will be recorded for this session.')
        return

    config._qatouch_reporter = reporter
    config.pluginmanager.register(reporter, 'qatouch_reporter')


class QATouchReporter:
    """Collects one result per node id and fans them out to the sinks."""

    def __init__(self, sinks: list[ResultSink]):
        self.sinks = sinks
        self.records: dict[str, RunResult] = {}
        # Whether anything this session does depends on resolving case keys.
        # It changes what a missing mapping policy costs, and therefore how
        # loudly it should be said.
        self.uploads = any(getattr(s, 'needs_case_keys', False) for s in sinks)
        self.mapping = self._load_mapping()
        # Parametrization-agnostic fallback. See qatouch.mapping.base_keyed:
        # stored keys carry [chromium], so a run under another browser (or an
        # Appium suite with no bracket) matches nothing without this.
        self.mapping_by_base = self._base_index(self.mapping)
        self.default_project_key = os.environ.get(
            'QATOUCH_PROJECT_ID', DEFAULT_PROJECT_KEY)
        self.capture_screenshots = any(
            getattr(s, 'wants_screenshots', False) for s in self.sinks)
        self.stats = {'mapped': 0, 'unmapped': 0}

    # -- mapping ------------------------------------------------------------

    def _load_mapping(self) -> dict[str, str]:
        """Load nodeid -> case_key through the consumer's injected policy.

        The policy decides which rows may upload. In the UI repo most
        mapping rows were auto-matched by word overlap - proposals, not
        links - so its policy passes only reviewed rows and reports how many
        it skipped. With no policy injected, or a policy that raises, this
        returns an empty mapping: nothing resolves to a case, and the reason
        is printed rather than swallowed.
        """
        if MAPPING_POLICY is None:
            cost = ('Nothing will be uploaded.' if self.uploads else
                    'The run record will carry no case keys.')
            print(f'QA Touch: no mapping policy was injected, so no node id '
                  f'can be resolved to a case. {cost} '
                  f'(The consumer shim sets MAPPING_POLICY.)')
            return {}

        path = MAPPING_PATH
        if path is None or not path.exists():
            log.warning(f'Mapping file not found: {path}')
            log.warning('Run: python tools/qatouch_mapper.py build')
            return {}

        try:
            reviewed, counts = MAPPING_POLICY(path)
        except Exception as e:
            cost = ('No results will be uploaded.' if self.uploads else
                    'The run record will carry no case keys.')
            print(f'QA Touch: could not load the mapping policy: {e}. {cost}')
            log.error(f'QA Touch: could not load the mapping policy: {e}. {cost}')
            return {}

        if counts.get('unreviewed'):
            log.warning(f'QA Touch: {counts["unreviewed"]} of {counts.get("total")} '
                        f'mapping row(s) are unreviewed proposals - tests on them '
                        f'run but do NOT upload. Review: '
                        f'python tools/qatouch_mapping.py list-unreviewed')
        return reviewed

    @staticmethod
    def _base_index(mapping: dict[str, str]) -> dict[str, str]:
        """Parametrization-collapsed view of the mapping, or empty on failure."""
        try:
            return base_keyed(mapping)
        except Exception as e:  # reporting must never fail a run
            log.warning(f'QA Touch: could not build the base mapping index: {e}')
            return {}

    def _resolve_case_key(self, item: Any, nodeid: str) -> str | None:
        """Marker first, then the mapping - exact node id, then the bare function."""
        if item is not None:
            marker = item.get_closest_marker('qatouch')
            if marker and marker.args:
                return marker.args[0]
        if nodeid in self.mapping:
            return self.mapping[nodeid]
        base = nodeid.split('[')[0]
        if base in self.mapping:
            return self.mapping[base]
        # Both sides collapsed to the bare function: matches a stored
        # [chromium] key when running under any other parametrization.
        return self.mapping_by_base.get(base)

    def _resolve_project_key(self, item: Any) -> str:
        """Order: per-test marker > env QATOUCH_PROJECT_ID > the default."""
        if item is not None:
            marker = item.get_closest_marker('qatouch_project')
            if marker and marker.args:
                return marker.args[0]
        return self.default_project_key

    # -- screenshots --------------------------------------------------------

    @staticmethod
    def _resolve_page(item: Any):
        """Find the Playwright Page object on a test item.

        Tries (1) class-based tests' self.page, (2) fixture funcargs `page`
        and `authenticated_page`. Returns None if no live page is available -
        the screenshot step is best-effort and never raises.
        """
        instance = getattr(item, 'instance', None)
        if instance is not None:
            page = getattr(instance, 'page', None)
            if page is not None:
                return page
        funcargs = getattr(item, 'funcargs', {}) or {}
        return funcargs.get('page') or funcargs.get('authenticated_page')

    def _maybe_capture_screenshot(self, item: Any, report: Any) -> None:
        """Capture a PNG on test failure, while fixtures are still alive."""
        if not self.capture_screenshots or report.outcome != 'failed':
            return
        page = self._resolve_page(item)
        if page is None:
            return
        try:
            # Viewport-only (not full_page) keeps payload size reasonable.
            item._qatouch_screenshot = page.screenshot()
        except Exception as e:
            log.debug(f'QA Touch: failed to capture screenshot for {item.nodeid}: {e}')

    # -- hooks --------------------------------------------------------------

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(self, item: Any, call: Any):
        outcome = yield
        report = outcome.get_result()

        if report.when != 'call':
            # A skip decided during setup never reaches the call phase.
            if report.when == 'setup' and report.skipped:
                self.record(item, item.nodeid, 'skipped',
                            duration=getattr(report, 'duration', None))
            return

        self._maybe_capture_screenshot(item, report)
        self.record(item, item.nodeid, report.outcome,
                    duration=getattr(report, 'duration', None))

    def record(self, item: Any, nodeid: str, outcome: str,
               duration: float | None = None) -> None:
        """Record one test's outcome under its node id.

        Every test is recorded, mapped or not: an unmapped failure still
        deserves a bug, and the node-id record is what the bug filer reads.
        """
        result = RunResult(
            nodeid=nodeid,
            outcome=outcome,
            duration=float(duration) if duration is not None else None,
            case_key=self._resolve_case_key(item, nodeid),
            project_key=self._resolve_project_key(item),
            screenshot=getattr(item, '_qatouch_screenshot', None) if item else None,
        )

        existing = self.records.get(nodeid)
        if existing is None:
            self.records[nodeid] = result
            if result.case_key:
                self.stats['mapped'] += 1
            else:
                self.stats['unmapped'] += 1
        else:
            # A rerun. Worse wins, and the duration and screenshot travel
            # with the outcome they belong to.
            self.records[nodeid] = merge_reruns(existing, result)

        for sink in self.sinks:
            try:
                sink.record(self.records[nodeid])
            except Exception as e:  # one sink must not cost the others
                log.warning(f'QA Touch: sink "{sink.name}" failed on record: {e}')

    def pytest_sessionfinish(self, session: Any, exitstatus: int) -> None:
        results = list(self.records.values())
        for sink in self.sinks:
            try:
                sink.finish(results)
            except Exception as e:  # one sink must not cost the others
                log.error(f'QA Touch: sink "{sink.name}" failed at session end: {e}')

    def pytest_terminal_summary(self, terminalreporter: Any, exitstatus: int) -> None:
        terminalreporter.write_sep('=', 'QA Touch results')
        mapped = self.stats['mapped']
        unmapped = self.stats['unmapped']
        # Node ids and cases are different counts, and where they differ a
        # single uploaded status stands for several tests. Show both.
        cases = len({r.case_key for r in self.records.values() if r.case_key})
        terminalreporter.write_line(
            f'  Mapped:     {mapped} {_tests(mapped)} -> {cases} '
            f'{"case" if cases == 1 else "cases"}')
        terminalreporter.write_line(
            f'  Unmapped:   {unmapped} {_tests(unmapped)} (no case mapping)')
        for sink in self.sinks:
            for line in sink.summary():
                terminalreporter.write_line(line)
