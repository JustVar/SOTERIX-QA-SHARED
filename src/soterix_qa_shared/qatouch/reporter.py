"""QA Touch Result Reporter -- pytest plugin.

Records what a test session produced and hands it to every registered sink.

Activation for the QA Touch upload:
  pytest --qatouch-upload --qatouch-run <RUN_KEY>
  or:  QATOUCH_UPLOAD=1  QATOUCH_RUN_KEY=<key>  pytest ...

The local run record is written on EVERY session, armed or not; the
flaky history, the run cross-check and the archive all read it.

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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from soterix_qa_shared.qatouch.layout import paths_for
from soterix_qa_shared.qatouch.mapping import base_keyed, canonical_nodeid, key_for
from soterix_qa_shared.qatouch.results import (
    ResultSink,
    RunContext,
    RunLedgerSink,
    RunResult,
    RunResultsFileSink,
    git_info,
    merge_reruns,
    relative_target,
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
# Tracked in git, unlike the paths above it.
HISTORY_DIR: Path | None = None

# policy(path: Path) -> (mapping, counts). Set by the consumer's shim before
# the module alias is installed. See the module docstring.
MAPPING_POLICY: Any = None


def configure(root: Path | str) -> None:
    """Point every writer path at `root`.

    Called from `pytest_configure` with `config.rootpath`, and by any CLI
    that resolves the repo root itself. Safe to call more than once; the
    last call wins.
    """
    global ROOT, MAPPING_PATH, FAILURE_LOG_DIR, FAILURE_LOG_PATH
    global RUN_RESULTS_PATH, HISTORY_DIR
    paths = paths_for(root)
    ROOT = paths['root']
    MAPPING_PATH = paths['mapping']
    FAILURE_LOG_DIR = paths['logs']
    FAILURE_LOG_PATH = paths['failure_log']
    RUN_RESULTS_PATH = paths['run_results']
    HISTORY_DIR = paths['history']


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
    group.addoption(
        '--no-history',
        action='store_true',
        default=False,
        help='Skip appending this run to the tracked history ledger.',
    )


def _tests(n: int) -> str:
    return 'test' if n == 1 else 'tests'


def _env_flag(name: str, default: str = '') -> bool:
    return os.environ.get(name, default).lower() in ('1', 'true', 'yes')


def is_xdist_worker() -> bool:
    """True inside a pytest-xdist worker process."""
    return bool(os.environ.get('PYTEST_XDIST_WORKER'))


# Options that walk the suite without running it. --setup-plan is not
# covered by --collect-only: measured, it still emits reports and wrote a
# 1-result record over a real 5-result one. Both consumer CI gates run
# --setup-plan, so listing only --collect-only would have left the guard
# half-open exactly where it is exercised most.
DRY_PASS_OPTIONS = ('--collect-only', '--setup-plan', '--setup-only')


def _is_dry_pass(config: Any) -> bool:
    """True when this invocation will not actually run any test body."""
    for option in DRY_PASS_OPTIONS:
        try:
            if config.getoption(option, default=False):
                return True
        except Exception as exc:
            # An option a future pytest renamed or dropped. Say so once:
            # a silently skipped guard here is a destroyed run record.
            log.debug(f'QA Touch: could not read {option}: {exc}')
    return False


def _armed_run_key(config: Any) -> str | None:
    """The run key of an ARMED session, or None.

    Arming needs both the flag and a run key: the uploader refuses to
    register without one, so a flag alone is not an armed run. The ledger
    keys off the same answer, which is what keeps "only armed runs are
    ledgered" from drifting away from what actually uploaded.
    """
    upload = (config.getoption('--qatouch-upload', default=False)
              or _env_flag('QATOUCH_UPLOAD'))
    run_key = (config.getoption('--qatouch-run', default=None)
               or os.environ.get('QATOUCH_RUN_KEY'))
    return run_key if (upload and run_key) else None


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
        sinks.append(RunResultsFileSink(path=RUN_RESULTS_PATH))
    # Registered on every run; it writes only on armed ones. Keeping that
    # rule inside the sink means it is stated once, beside the reason.
    if not config.getoption('--no-history', default=False):
        sinks.append(RunLedgerSink(directory=HISTORY_DIR))
    if upload_sink:
        sinks.append(upload_sink)
    return sinks


def pytest_configure(config: Any) -> None:
    # A collection-only pass produces no results, and writing its empty
    # record over a real one destroys the last real run. Measured: a bare
    # `pytest --collect-only` replaced a 20-result record with zero, and no
    # reader could tell - run_suite checks only that the file exists, so an
    # empty one reads as "a run with no failures" rather than "no run".
    #
    # Four shipped tools run a bare collect-only pass after a suite
    # (run_suite, qatouch_mapper, qatouch_sync_check, qatouch_run_manager),
    # and so does a merge gate. Both sibling repo-writing plugins in the UI
    # repo carry this guard; this one did not, which the sink split made
    # destructive rather than merely absent.
    if _is_dry_pass(config):
        return

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
        reporter = QATouchReporter(
            sinks,
            run_key=_armed_run_key(config),
            project_key=os.environ.get('QATOUCH_PROJECT_ID', DEFAULT_PROJECT_KEY),
            target=relative_target(getattr(config, 'args', ()) or (),
                                   config.rootpath),
        )
    except Exception as exc:
        print(f'QA Touch: the reporter could not start ({exc}). '
              f'No results will be recorded for this session.')
        return

    config._qatouch_reporter = reporter
    config.pluginmanager.register(reporter, 'qatouch_reporter')


def product_for_nodeid(nodeid: str, default_key: str | None) -> str:
    """The project a node id reports to when no item is in reach (the
    controller's logreport under xdist): the path rule, else the default."""
    from soterix_qa_shared.reporting.products import product_for
    return product_for(nodeid, None, default_key)


class QATouchReporter:
    """Collects one result per node id and fans them out to the sinks."""

    def __init__(self, sinks: list[ResultSink], run_key: str | None = None,
                 project_key: str | None = None, target: tuple[str, ...] = ()):
        self.sinks = sinks
        self.records: dict[str, RunResult] = {}
        # Wall clock, not the sum of test durations: collection, fixtures and
        # teardown are the difference, and they are often most of it.
        self.started = datetime.now(UTC)
        self.run_key = run_key
        self.target = target
        # Whether anything this session does depends on resolving case keys.
        # It changes what a missing mapping policy costs, and therefore how
        # loudly it should be said.
        self.uploads = any(getattr(s, 'needs_case_keys', False) for s in sinks)
        self.mapping = self._load_mapping()
        # Parametrization-agnostic fallback. See qatouch.mapping.base_keyed:
        # stored keys carry [chromium], so a run under another browser (or an
        # Appium suite with no bracket) matches nothing without this.
        self.mapping_by_base = self._base_index(self.mapping)
        self.default_project_key = project_key or os.environ.get(
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
            # Name the FILE, not a command. Each consumer reviews rows with
            # its own tooling and the package cannot know which - the UI
            # repo has a list-unreviewed CLI, the API repo does not, and a
            # message naming a command that is not there is the one line a
            # reader meets at the moment they want to act on it.
            log.warning(f'QA Touch: {counts["unreviewed"]} of {counts.get("total")} '
                        f'mapping row(s) are unreviewed proposals - tests on them '
                        f'run but do NOT upload. They are the rows without '
                        f'"reviewed": true in {path}')
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
        """Marker first, then the mapping - exact node id, then the bare function.

        A mapping value is one key, or one key per project (qatouch.mapping
        .key_for): the key returned is the one for the project this test
        reports to, so a Core test run against Neurosen writes to the
        Neurosen case, and to nothing when the row has no key for it.
        """
        if item is not None:
            marker = item.get_closest_marker('qatouch')
            if marker and marker.args:
                return marker.args[0]
        value = self._mapping_value(nodeid)
        if value is None:
            return None
        return key_for(value, self._resolve_project_key(item) if item is not None
                       else product_for_nodeid(nodeid, self.default_project_key))

    def _mapping_value(self, nodeid: str):
        # pytest-xdist reports a grouped test as '...[chromium]@<group>'; the mapping has no group
        nodeid = canonical_nodeid(nodeid)
        if nodeid in self.mapping:
            return self.mapping[nodeid]
        base = nodeid.split('[')[0]
        if base in self.mapping:
            return self.mapping[base]
        # Both sides collapsed to the bare function: matches a stored
        # [chromium] key when running under any other parametrization.
        return self.mapping_by_base.get(base)

    def _resolve_project_key(self, item: Any) -> str:
        """Order: per-test marker > the consumer's path rule > env
        QATOUCH_PROJECT_ID > the default.

        The same precedence the archive uses to name a run's product
        (reporting/products.product_for), so a c2c test archived as Camera
        2 Cloud also uploads to Camera 2 Cloud. Until 09/11/2026 the
        upload skipped the path rule and a c2c run would have gone to the
        Core project.
        """
        from soterix_qa_shared.reporting.products import product_for
        marker_value = None
        if item is not None:
            marker = item.get_closest_marker('qatouch_project')
            if marker and marker.args:
                marker_value = str(marker.args[0])
        nodeid = getattr(item, 'nodeid', '') if item is not None else ''
        return product_for(nodeid, marker_value, self.default_project_key)

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
        """Attach what only the RUNNING process can see to the report.

        Under xdist, this hook fires on the worker and never on the
        controller (measured). The item, its markers and its live Playwright
        page exist only here, so everything derived from them is stashed on
        the report, which xdist serialises to the controller intact -
        including the screenshot bytes (also measured).
        """
        outcome = yield
        report = outcome.get_result()
        if report.when != 'call' and not (report.when == 'setup' and report.skipped):
            return
        self._maybe_capture_screenshot(item, report)
        try:
            report._qatouch = {
                'case_key': self._resolve_case_key(item, item.nodeid),
                'project_key': self._resolve_project_key(item),
                'screenshot': getattr(item, '_qatouch_screenshot', None),
            }
        except Exception as e:  # a broken marker must not fail the test
            log.warning(f'QA Touch: could not resolve {item.nodeid}: {e}')

    def pytest_runtest_logreport(self, report: Any) -> None:
        """Record the result. This is the hook the CONTROLLER sees:
        makereport runs only on the workers under xdist, while logreport
        fires on the controller with every test, parallel or serial.
        """
        if report.when == 'call':
            outcome = report.outcome
        elif report.when == 'setup' and report.skipped:
            # A skip decided during setup never reaches the call phase.
            outcome = 'skipped'
        elif report.failed:
            # An ERROR in setup or teardown. Measured before this branch
            # existed: a test whose fixture raised on the way IN was not
            # recorded at all, and one whose fixture raised on the way OUT
            # was recorded 'passed' - so an armed run uploaded a Passed to
            # QA Touch for a test that errored. A whole suite failing in
            # setup, which is what "the environment is down" looks like,
            # produced an empty record indistinguishable from a typo.
            #
            # Recorded as failed, and worst-wins in record_result means a
            # teardown error correctly overrides the passing call phase.
            outcome = 'failed'
        else:
            return
        extra = getattr(report, '_qatouch', None) or {}
        self.record_result(RunResult(
            nodeid=report.nodeid,
            outcome=outcome,
            duration=float(getattr(report, 'duration', 0.0) or 0.0),
            case_key=extra.get('case_key'),
            project_key=extra.get('project_key') or self.default_project_key,
            screenshot=extra.get('screenshot'),
        ))

    def record(self, item: Any, nodeid: str, outcome: str,
               duration: float | None = None) -> None:
        """Record one test's outcome under its node id.

        Every test is recorded, mapped or not: an unmapped failure still
        deserves a bug, and the node-id record is what the bug filer reads.
        """
        self.record_result(RunResult(
            nodeid=nodeid,
            outcome=outcome,
            duration=float(duration) if duration is not None else None,
            case_key=self._resolve_case_key(item, nodeid),
            project_key=self._resolve_project_key(item),
            screenshot=getattr(item, '_qatouch_screenshot', None) if item else None,
        ))

    def record_result(self, result: RunResult) -> None:
        """Store one already-built result, worst-wins per node id."""
        nodeid = result.nodeid
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

    def run_context(self, exitstatus: int | None = None) -> RunContext:
        branch, commit = git_info(ROOT) if ROOT else (None, None)
        return RunContext(
            started=self.started,
            ended=datetime.now(UTC),
            env=os.environ.get('ENV', 'unknown'),
            box=os.environ.get('BOX', ''),
            target=self.target,
            exitstatus=exitstatus,
            run_key=self.run_key,
            project_key=self.default_project_key,
            branch=branch,
            commit=commit,
        )

    def pytest_sessionfinish(self, session: Any, exitstatus: int) -> None:
        # Only the controller writes. Under xdist this hook fires in every
        # worker AND in the controller (measured), so without this each
        # worker would write its own subset over the others, and the
        # controller would write an empty one over all of them. The
        # controller sees every result through logreport.
        if is_xdist_worker():
            return
        results = list(self.records.values())
        try:
            context = self.run_context(exitstatus)
        except Exception as e:  # never let provenance cost a run its results
            log.warning(f'QA Touch: could not build the run context: {e}')
            context = RunContext(started=self.started, ended=self.started,
                                 run_key=self.run_key)
        for sink in self.sinks:
            try:
                sink.finish(results, context)
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
