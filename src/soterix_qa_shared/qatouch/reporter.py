"""QA Touch Result Reporter -- pytest plugin.

Uploads test results to QA Touch after a test session completes.
Requires a test run to exist in QA Touch (create one first).

Activation:
  pytest --qatouch-upload --qatouch-run <RUN_KEY>

Or via env vars:
  QATOUCH_UPLOAD=1  QATOUCH_RUN_KEY=<key>  pytest ...

The plugin:
  1. Loads the node id -> case_key mapping through the injected policy
  2. Collects pass/fail/skip results per test via pytest hooks
  3. At session end, batch-uploads results to QA Touch with retry

Resilience:
  - Upload retries 3 times with exponential backoff (2s, 4s, 8s)
  - On final failure, writes results to logs/upload_failures.json for later retry
  - Never crashes the test process - upload errors are logged, not raised

Status codes: passed=1, blocked/skipped=3, failed=5

Two things this module deliberately does NOT own, because it now lives in
site-packages rather than in a test repo:

  * **Paths.** Every writer path derives from a root the consumer supplies
    through `configure(root)`. `__file__` points into the installed package,
    so it can no longer stand in for the repo root.
  * **Mapping policy.** Which mapping rows may upload is a per-repo decision
    (the UI gates on a `reviewed` flag; the API does not). The consumer's
    shim assigns `MAPPING_POLICY` before the module alias is installed. With
    no policy the reporter degrades to no-upload and says so out loud -
    it never guesses a permissive default, because an unreviewed guess
    writes a real status onto a case no test verifies.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from soterix_qa_shared.qatouch.mapping import base_keyed

log = logging.getLogger('qatouch_reporter')

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
# Per-nodeid run results dumped at session-finish; consumed by the downstream
# Jira bug filer (tools/run_suite.py and friends). Lives under logs/ alongside
# the upload-failure log so both are gitignored together.
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
    ROOT = Path(root)
    MAPPING_PATH = ROOT / 'config' / 'qatouch_case_mapping.json'
    FAILURE_LOG_DIR = ROOT / 'logs'
    FAILURE_LOG_PATH = FAILURE_LOG_DIR / 'upload_failures.json'
    RUN_RESULTS_PATH = FAILURE_LOG_DIR / 'last_run_results.json'


# Retry configuration
MAX_RETRIES = 3
RETRY_BASE_DELAY = 2  # seconds, doubles each attempt

# Per-file limit for result attachments. The code endpoint accepts multipart
# file[] parts (png/pdf/jpg/csv/log) up to 2 MB each - see
# docs/QA_TOUCH_INTEGRATION.md. Enforced here before anything is sent: an
# oversized file is skipped whole, never truncated, because a cut PNG uploads
# fine and opens broken - worse than absent.
MAX_ATTACHMENT_BYTES = 2 * 1024 * 1024

# QA Touch status codes
STATUS_PASSED = 1
STATUS_BLOCKED = 3
STATUS_FAILED = 5

PYTEST_TO_QT = {
    'passed': STATUS_PASSED,
    'failed': STATUS_FAILED,
    'skipped': STATUS_BLOCKED,
}


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


def pytest_configure(config: Any) -> None:
    # Paths first, before the arming check: a CLI or a later hook may need
    # them even on a run that never uploads, and resolving them is cheap.
    # Guarded because an exception raised here aborts the whole session with
    # INTERNALERROR - seen for real when a Windows 8.3-vs-long path made
    # rootdir fall back to C:\Users.
    try:
        configure(config.rootpath)
    except Exception as exc:  # reporting must never kill a run
        print(f'QA Touch: could not resolve the repo root ({exc}). '
              f'Result reporting is off for this session.')
        return

    upload = config.getoption('--qatouch-upload', default=False) or os.environ.get('QATOUCH_UPLOAD', '').lower() in ('1', 'true', 'yes')
    if not upload:
        return

    run_key = config.getoption('--qatouch-run', default=None) or os.environ.get('QATOUCH_RUN_KEY')
    if not run_key:
        log.warning('QA Touch upload enabled but no test run key provided. '
                     'Use --qatouch-run <key> or QATOUCH_RUN_KEY env var.')
        return

    try:
        reporter = QATouchReporter(run_key)
    except Exception as exc:  # see the INTERNALERROR note above
        print(f'QA Touch: the reporter could not start ({exc}). '
              f'No results will be uploaded for this session.')
        return

    config._qatouch_reporter = reporter
    config.pluginmanager.register(reporter, 'qatouch_reporter')


def _format_duration(seconds: float) -> str:
    """Format pytest duration for QA Touch's `execution_time` field.

    Matches the format the vendor playwright-qatouch-reporter emits (e.g. "1.2s",
    "450ms") so values are comparable in the QA Touch UI.
    """
    if seconds is None or seconds < 0:
        return ''
    if seconds < 1.0:
        return f'{round(seconds * 1000)}ms'
    return f'{seconds:.1f}s'


_OUTCOME_RANK = {'passed': 0, 'skipped': 1, 'failed': 2}


def _worse_outcome(new: str, existing: str | None) -> bool:
    """Return True if `new` outcome should overwrite `existing` (worse wins)."""
    return _OUTCOME_RANK.get(new, -1) > _OUTCOME_RANK.get(existing or '', -1)


DEFAULT_PROJECT_KEY = 'JlW1'


class QATouchReporter:
    def __init__(self, run_key: str):
        self.run_key = run_key
        self.project_key = os.environ.get('QATOUCH_PROJECT_ID', DEFAULT_PROJECT_KEY)
        self.results: dict[str, int] = {}  # case_key -> status code
        # case_key -> project_key resolved at record time (per-test marker override)
        self.case_projects: dict[str, str] = {}
        self.durations: dict[str, float] = {}  # case_key -> duration in seconds
        self.screenshots: dict[str, bytes] = {}  # case_key -> PNG bytes (failures only)
        self.nodeid_outcomes: dict[str, dict] = {}  # nodeid -> {outcome, duration, case_key}
        self.mapping = self._load_mapping()
        # Parametrization-agnostic fallback. See qatouch.mapping.base_keyed:
        # stored keys carry [chromium], so a run under another browser (or an
        # Appium suite with no bracket) matches nothing without this.
        self.mapping_by_base = self._base_index(self.mapping)
        self.case_to_run_key: dict[str, str] = {}  # case_key -> run_key (for API-created runs)
        self.use_playwright_endpoint = os.environ.get(
            'QATOUCH_USE_PLAYWRIGHT_ENDPOINT', '').lower() in ('1', 'true', 'yes')
        # Failure-screenshot capture is enabled by default for the playwright
        # endpoint, since legacy uploads ignore attachments anyway.
        self.capture_screenshots = self.use_playwright_endpoint and (
            os.environ.get('QATOUCH_CAPTURE_SCREENSHOTS', '1').lower()
            not in ('0', 'false', 'no'))
        self.stats = {'mapped': 0, 'unmapped': 0, 'uploaded': 0, 'errors': 0,
                      'retries': 0, 'saved_for_retry': 0, 'screenshots': 0}
        # Set by pytest_sessionfinish once an upload is attempted.
        self._upload_path_used: str = '(none)'

    def _load_mapping(self) -> dict[str, str]:
        """Load nodeid -> case_key through the consumer's injected policy.

        The policy decides which rows may upload. In the UI repo most
        mapping rows were auto-matched by word overlap - proposals, not
        links - so its policy passes only reviewed rows and reports how
        many it skipped. With no policy injected, or a policy that raises,
        this returns an empty mapping: nothing uploads, and the reason is
        printed rather than swallowed.
        """
        if MAPPING_POLICY is None:
            print('QA Touch: no mapping policy was injected, so no node id '
                  'can be resolved to a case. Nothing will be uploaded. '
                  '(The consumer shim sets MAPPING_POLICY.)')
            return {}

        path = MAPPING_PATH
        if path is None or not path.exists():
            log.warning(f'Mapping file not found: {path}')
            log.warning('Run: python tools/qatouch_mapper.py build')
            return {}

        try:
            reviewed, counts = MAPPING_POLICY(path)
        except Exception as e:  # a broken policy must not upload
            print(f'QA Touch: could not load the mapping policy: {e}. '
                  f'No results will be uploaded.')
            log.error(f'QA Touch: could not load the mapping policy: {e}. '
                      f'No results will be uploaded.')
            return {}

        if counts.get('unreviewed'):
            log.warning(f'QA Touch: {counts["unreviewed"]} of {counts.get("total")} mapping '
                        f'row(s) are unreviewed proposals - tests on them run but do '
                        f'NOT upload. Review: python tools/qatouch_mapping.py list-unreviewed')
        return reviewed

    def _build_run_key_mapping(self, client) -> None:
        """Build case_key -> full case CODE (e.g. "TR0263") for this run.

        The results API identifies a case by its case code, NOT by the case
        key and NOT by the per-result run_key. Measured 2026-08-17: sending
        either of those returns {"success": true} and changes nothing, so a
        broken upload is indistinguishable from a working one. This mapping
        is what makes an upload actually land.

        The run's own result rows carry the code, so this needs no extra call.
        """
        try:
            all_results = None
            for attempt in range(3):
                try:
                    all_results = client.get_all_test_run_results(
                        self.project_key, self.run_key)
                    break
                except Exception as e:
                    # The public API rate-limits bursts; a 429 here would
                    # otherwise turn the whole upload into a silent no-op.
                    if '429' in str(e) and attempt < 2:
                        log.warning(f'QA Touch: rate limited reading the run - '
                                    f'waiting 30s (attempt {attempt + 1}/3)')
                        time.sleep(30)
                        continue
                    raise
            for r in all_results:
                case_id = r.get('case_id', '')
                code = r.get('code', '')
                if case_id and code:
                    self.case_to_run_key[case_id] = code
            if self.case_to_run_key:
                log.info(f'QA Touch: Resolved case codes for {len(self.case_to_run_key)} cases '
                         f'in run {self.run_key}')
            else:
                log.error('QA Touch: Could not resolve any case codes for this run. '
                          'Uploads would silently do nothing, so they will be skipped.')
        except Exception as e:
            log.error(f'QA Touch: Could not build the case-code mapping: {e}. '
                      f'Uploads would silently do nothing, so they will be skipped.')

    @staticmethod
    def _base_index(mapping: dict[str, str]) -> dict[str, str]:
        """Parametrization-collapsed view of the mapping, or empty on failure."""
        try:
            return base_keyed(mapping)
        except Exception as e:  # reporting must never fail a run
            log.warning(f'QA Touch: could not build the base mapping index: {e}')
            return {}

    def _nodeid_variants(self, nodeid: str) -> list[str]:
        """Generate nodeid variants for flexible matching.

        Playwright tests have [chromium] suffix, parametrized tests have [param].
        Try exact match first, then without parameters.
        """
        variants = [nodeid]
        # Strip browser parameter: tests/ui/views/test_views_basic.py::TestClass::test_func[chromium]
        if '[' in nodeid:
            base = nodeid.split('[')[0]
            variants.append(base)
        return variants

    @staticmethod
    def _get_marker_case_key(item: Any) -> str | None:
        """Read case_key from @pytest.mark.qatouch("KEY") marker on the test item."""
        marker = item.get_closest_marker('qatouch')
        if marker and marker.args:
            return marker.args[0]
        return None

    @staticmethod
    def _get_marker_project_key(item: Any) -> str | None:
        """Read project_key from @pytest.mark.qatouch_project("PKEY") on the test item.

        Per-test marker is authoritative; falls back to the reporter's default
        (env QATOUCH_PROJECT_ID or DEFAULT_PROJECT_KEY) when absent.
        """
        if item is None:
            return None
        marker = item.get_closest_marker('qatouch_project')
        if marker and marker.args:
            return marker.args[0]
        return None

    def _resolve_project_key(self, item: Any) -> str:
        """Resolve project key for a given test item.

        Order: per-test marker > env QATOUCH_PROJECT_ID > DEFAULT_PROJECT_KEY.
        self.project_key already encodes env-vs-default, so we only check marker
        and fall back to self.project_key.
        """
        marker_project = self._get_marker_project_key(item)
        return marker_project or self.project_key

    @staticmethod
    def _resolve_page(item: Any):
        """Find the Playwright Page object on a test item.

        Tries (1) class-based tests' self.page, (2) fixture funcargs `page` and
        `authenticated_page`. Returns None if no live page is available - the
        screenshot step is best-effort and never raises.
        """
        instance = getattr(item, 'instance', None)
        if instance is not None:
            page = getattr(instance, 'page', None)
            if page is not None:
                return page
        funcargs = getattr(item, 'funcargs', {}) or {}
        return funcargs.get('page') or funcargs.get('authenticated_page')

    def _maybe_capture_screenshot(self, item: Any, report: Any) -> None:
        """Capture a PNG screenshot on test failure for QA Touch upload.

        Runs inside the makereport hookwrapper while fixtures are still alive.
        Stores raw bytes on the item; _record_result moves it to self.screenshots
        once the case_key is resolved.
        """
        if not self.capture_screenshots:
            return
        if report.outcome != 'failed':
            return
        page = self._resolve_page(item)
        if page is None:
            return
        try:
            # Viewport-only (not full_page) keeps payload size reasonable.
            png = page.screenshot()
            item._qatouch_screenshot = png
        except Exception as e:
            log.debug(f'QA Touch: failed to capture screenshot for {item.nodeid}: {e}')

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(self, item: Any, call: Any):
        outcome = yield
        report = outcome.get_result()

        # Only care about the 'call' phase (not setup/teardown)
        if report.when != 'call':
            # Handle skips in setup phase
            if report.when == 'setup' and report.skipped:
                self._record_result(item, item.nodeid, 'skipped',
                                    duration=getattr(report, 'duration', None))
            return

        self._maybe_capture_screenshot(item, report)
        self._record_result(item, item.nodeid, report.outcome,
                            duration=getattr(report, 'duration', None))

    def _record_result(self, item: Any, nodeid: str, outcome: str,
                       duration: float | None = None) -> None:
        """Map a pytest outcome to a QA Touch result.

        Resolution order:
          1. @pytest.mark.qatouch("KEY") marker on the test (authoritative)
          2. JSON mapping file fallback (legacy, for unmarked tests)
        """
        # Primary: read from marker
        case_key = self._get_marker_case_key(item) if item else None

        # Fallback: JSON mapping
        if not case_key:
            for variant in self._nodeid_variants(nodeid):
                if variant in self.mapping:
                    case_key = self.mapping[variant]
                    break
            else:
                # Both sides collapsed to the bare function: matches a stored
                # [chromium] key when running under any other parametrization.
                base = nodeid.split('[')[0]
                case_key = self.mapping_by_base.get(base)

        # Always record the per-nodeid outcome - the post-run bug filer keys off
        # nodeid (not case_key), and unmapped failures still deserve a bug.
        existing_nodeid = self.nodeid_outcomes.get(nodeid)
        if existing_nodeid is None or _worse_outcome(outcome, existing_nodeid.get('outcome')):
            self.nodeid_outcomes[nodeid] = {
                'outcome': outcome,
                'duration': float(duration) if duration is not None else None,
                'case_key': case_key,
            }

        if not case_key:
            self.stats['unmapped'] += 1
            return

        status = PYTEST_TO_QT.get(outcome)
        if status is None:
            return

        # If already recorded (e.g., reruns), keep the worst result
        existing = self.results.get(case_key)
        if existing is not None:
            # Failed > Blocked > Passed (higher number = worse)
            status = max(existing, status)

        self.results[case_key] = status
        # Record per-test project resolution. If the same case_key is recorded
        # under two different projects (rerun with a different marker), keep the
        # last one - markers should be stable per case_key in practice.
        self.case_projects[case_key] = self._resolve_project_key(item)
        if duration is not None:
            # Keep the latest duration (most recent run wins on reruns)
            self.durations[case_key] = float(duration)
        screenshot = getattr(item, '_qatouch_screenshot', None) if item else None
        if screenshot:
            self.screenshots[case_key] = screenshot
            self.stats['screenshots'] += 1
        self.stats['mapped'] += 1

    def _upload_with_retry(self, upload_fn) -> bool:
        """Run an upload callable with retry logic. Returns True on success."""
        last_error = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                upload_fn()
                if attempt > 1:
                    log.info(f'QA Touch: Upload succeeded on attempt {attempt}')
                    self.stats['retries'] += attempt - 1
                return True
            except Exception as e:
                last_error = e
                if attempt < MAX_RETRIES:
                    delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                    log.warning(f'QA Touch: Upload attempt {attempt}/{MAX_RETRIES} failed: {e}. '
                                f'Retrying in {delay}s...')
                    self.stats['retries'] += 1
                    time.sleep(delay)
                else:
                    log.error(f'QA Touch: Upload failed after {MAX_RETRIES} attempts: {last_error}')
        return False

    def _verify_upload(self, client, results_payload: list) -> bool:
        """Read the run back and confirm the statuses actually changed.

        QA Touch reports success for uploads it silently discards, so an
        upload is not done until the run agrees. Returns True if every
        uploaded case now carries the status we sent.
        """
        report = None
        for attempt in range(3):
            try:
                report = client.verify_results_landed(
                    self.project_key, self.run_key,
                    [{'case': r['case'], 'status': r['status']} for r in results_payload])
                break
            except Exception as e:
                if '429' in str(e) and attempt < 2:
                    log.warning(f'QA Touch: rate limited during verify - '
                                f'waiting 30s (attempt {attempt + 1}/3)')
                    time.sleep(30)
                    continue
                log.warning(f'QA Touch: could not verify the upload: {e}')
                return True  # do not fail the run over a read-back problem

        problems = report['mismatched'] + [f'{c}: not in run' for c in report['missing']]
        if problems:
            log.error(f'QA Touch: {len(problems)} result(s) did not land. '
                      f'First few: {problems[:5]}')
            return False
        log.info(f'QA Touch: verified {len(report["landed"])} result(s) in the run.')
        return True

    def _save_failed_results(self, results_payload: list, comment: str, error: str) -> None:
        """Save failed upload results to JSON for later retry.

        The entry shape is a contract with `soterix_qa_shared.qatouch.retry`
        and each repo's `validate_run` - see FAILURE_ENTRY_FIELDS.
        """
        try:
            FAILURE_LOG_DIR.mkdir(parents=True, exist_ok=True)

            # Load existing failures (append, don't overwrite)
            existing = []
            if FAILURE_LOG_PATH.exists():
                try:
                    with open(FAILURE_LOG_PATH) as f:
                        existing = json.load(f)
                except (json.JSONDecodeError, ValueError):
                    existing = []

            failure_entry = {
                'timestamp': datetime.now(UTC).isoformat(),
                'project_key': self.project_key,
                'run_key': self.run_key,
                'result_count': len(results_payload),
                'results': results_payload,
                'comment': comment,
                'error': str(error),
            }
            existing.append(failure_entry)

            with open(FAILURE_LOG_PATH, 'w') as f:
                json.dump(existing, f, indent=2)

            self.stats['saved_for_retry'] = len(results_payload)
            log.info(f'QA Touch: Saved {len(results_payload)} results to '
                     f'{FAILURE_LOG_PATH} for later retry')
        except Exception as save_err:
            log.error(f'QA Touch: Failed to save results for retry: {save_err}')

    def _write_run_results(self) -> None:
        """Dump per-nodeid outcomes to last_run_results.json for run_suite.py.

        Written at session-finish regardless of upload success so the post-run
        bug filer can run even when QA Touch upload had errors. Includes both
        mapped and unmapped tests (unmapped tests will have case_key=None).

        The payload shape is a contract with each repo's run_archive,
        run_report, run_suite and validate_run - see RUN_RESULTS_FIELDS.
        """
        try:
            FAILURE_LOG_DIR.mkdir(parents=True, exist_ok=True)
            records = [
                {
                    'nodeid': nodeid,
                    'outcome': rec.get('outcome'),
                    'duration': rec.get('duration'),
                    'case_key': rec.get('case_key'),
                }
                for nodeid, rec in self.nodeid_outcomes.items()
            ]
            payload = {
                'timestamp': datetime.now(UTC).isoformat(),
                'run_key': self.run_key,
                'project_key': self.project_key,
                'env': os.environ.get('ENV', 'unknown'),
                'results': records,
            }
            with open(RUN_RESULTS_PATH, 'w') as f:
                json.dump(payload, f, indent=2)
            log.info(f'QA Touch: Wrote {len(records)} per-nodeid records to '
                     f'{RUN_RESULTS_PATH}')
        except Exception as e:
            log.warning(f'QA Touch: Failed to write run results JSON: {e}')

    def _group_results_by_project(self) -> dict[str, list[str]]:
        """Group recorded case_keys by their resolved project_key.

        Cases with no recorded project (legacy path / no marker) fall under
        self.project_key. Returns {project_key: [case_key, ...]}.
        """
        groups: dict[str, list[str]] = {}
        for case_key in self.results:
            pk = self.case_projects.get(case_key, self.project_key)
            groups.setdefault(pk, []).append(case_key)
        return groups

    def _build_payload(self, case_keys: list[str]) -> list[dict]:
        """Build the results payload for a subset of case_keys."""
        results_payload = []
        translated = 0
        for case_key in case_keys:
            status = self.results[case_key]
            upload_key = self.case_to_run_key.get(case_key)
            if not upload_key:
                # Sending the case key instead would be accepted and ignored.
                log.warning(f'QA Touch: no case code for {case_key} in run '
                            f'{self.run_key} - skipping it rather than sending '
                            f'an identifier the API ignores.')
                continue
            translated += 1
            entry = {'case': upload_key, 'status': status}
            if self.use_playwright_endpoint:
                duration = self.durations.get(case_key)
                entry['execution_time'] = _format_duration(duration) if duration is not None else ''
                entry['comments'] = ''
                entry['description'] = ''
                attachments = []
                png = self.screenshots.get(case_key)
                if png:
                    attachments.append({
                        'name': f'{case_key}-failure.png',
                        'contentType': 'image/png',
                        'content': base64.b64encode(png).decode('ascii'),
                    })
                entry['attachments'] = attachments
            results_payload.append(entry)

        if translated > 0:
            log.info(
                f'QA Touch: Translated {translated} case_keys to run_keys '
                '(API-created run)')
        return results_payload

    def _collect_attachment_parts(self, case_keys: list[str]) -> dict[str, tuple[str, bytes, str]]:
        """Build multipart file parts for failed cases with captured screenshots.

        Returns {case_key: (filename, content, content_type)} with the tuple
        in the shape QATouchClient.update_test_run_results_by_code takes.
        Keyed by case_key so the caller knows which cases actually ride -
        the timing comment covers exactly those. Files are named by case
        CODE (TR0263-failure.png) because the run UI shows codes, so a
        reader can match a file to its case without the mapping.

        A file over MAX_ATTACHMENT_BYTES is skipped whole with a log line.
        A case with no resolved code is skipped too - _build_payload already
        dropped its result, so there is nothing for a file to hang on.
        """
        parts: dict[str, tuple[str, bytes, str]] = {}
        for case_key in case_keys:
            png = self.screenshots.get(case_key)
            if not png:
                continue
            if self.results.get(case_key) != STATUS_FAILED:
                # Screenshots are captured on a failed call phase; if a rerun
                # later improved the recorded status, the picture no longer
                # matches the result being uploaded.
                continue
            code = self.case_to_run_key.get(case_key)
            if not code:
                continue
            if len(png) > MAX_ATTACHMENT_BYTES:
                log.warning(f'QA Touch: screenshot for {code} is {len(png)} '
                            f'bytes, over the 2 MB attachment cap - skipping '
                            f'it (never truncated).')
                continue
            parts[case_key] = (f'{code}-failure.png', png, 'image/png')
        return parts

    def _timing_comment_lines(self, case_keys: list[str]) -> list[str]:
        """Per-case execution times for the upload comment.

        One line per given case with a recorded duration, e.g.
        "TR0263 execution: 1.2s". The comments field is the only documented
        place for timing on the code endpoint - the result JSON carries
        case and status alone. The client sends comments in the URL query
        string, so the caller passes only the attached cases: a line for
        every uploaded case would grow the URL with run size and risk
        rejection on exactly the large runs attachments target.
        """
        lines: list[str] = []
        for case_key in case_keys:
            duration = self.durations.get(case_key)
            code = self.case_to_run_key.get(case_key)
            if duration is None or not code:
                continue
            lines.append(f'{code} execution: {_format_duration(duration)}')
        return lines

    def pytest_sessionfinish(self, session: Any, exitstatus: int) -> None:
        """Upload collected results to QA Touch."""
        # Write per-nodeid results JSON for the post-run bug filer first - this
        # path must run even if there are no mapped uploads.
        self._write_run_results()

        if not self.results:
            log.info('QA Touch: No mapped results to upload.')
            return

        # Group cases by their resolved project key. Any project_key != the
        # reporter's default needs its own run_key - we don't have one, so we
        # log clearly and skip those uploads.
        project_groups = self._group_results_by_project()
        cross_project = [pk for pk in project_groups if pk != self.project_key]
        if cross_project:
            log.error(
                f'QA Touch: Cases target non-default projects {cross_project} '
                f'but no per-project run key is configured. Default project: '
                f'{self.project_key}, run: {self.run_key}. Skipping those cases.')
            for pk in cross_project:
                self.stats['errors'] += 1
                skipped = project_groups.pop(pk)
                log.error(
                    f'QA Touch: Skipped {len(skipped)} case(s) for project '
                    f'{pk}: {skipped}')

        # Now only self.project_key remains in groups (or empty).
        case_keys = project_groups.get(self.project_key, [])
        if not case_keys:
            log.info('QA Touch: No uploadable mapped results after project filtering.')
            return

        timestamp = datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')
        comment = f'Automated upload from Playwright run at {timestamp}'

        # Upload via the QA Touch client.
        try:
            from soterix_qa_shared.qatouch import QATouchClient
            client = QATouchClient()
        except Exception as e:
            log.error(f'QA Touch: Failed to initialize client: {e}')
            self.stats['errors'] += 1
            return

        # Build run_key mapping for API-created runs
        try:
            self._build_run_key_mapping(client)
        except Exception as e:
            log.warning(f'QA Touch: run_key mapping failed (non-fatal): {e}')

        results_payload = self._build_payload(case_keys)

        if not results_payload:
            # Every mapped result failed to resolve to a case CODE. Sending
            # case keys instead would be accepted and ignored (trap 3), and
            # an empty POST would "verify" trivially - either way the summary
            # would claim success while nothing was written. That is the
            # silent-no-op class this reporter exists to prevent, so this is
            # an upload FAILURE, saved for retry, not an empty success.
            self.stats['errors'] += 1
            self._save_failed_results(
                [{'case': k, 'status': self.results[k]} for k in case_keys],
                comment,
                'No case codes resolved for this run - nothing was sent. '
                'tools/retry_uploads.py resolves the keys and replays.')
            log.error(f'QA Touch: could not resolve ANY of the {len(case_keys)} '
                      f'mapped case(s) to case codes for run {self.run_key}. '
                      f'NOTHING was uploaded. Saved to the failure log.')
            self._upload_path_used = 'failed (no case codes)'
            return

        endpoint_label = 'playwright' if self.use_playwright_endpoint else 'legacy'
        log.info(f'QA Touch: Uploading {len(results_payload)} results to run '
                 f'{self.run_key} via {endpoint_label} endpoint...')
        self._upload_path_used = endpoint_label

        plain_results = [{'case': r['case'], 'status': r['status']}
                         for r in results_payload]

        # The playwright path also sends what the run captured: failure
        # screenshots as multipart file[] parts, and execution times for
        # the ATTACHED cases in the comment. The comment rides the URL
        # query string, so timing stays bounded by failure count - a line
        # per uploaded case would grow the URL with run size. Both ride
        # only when a screenshot survived the cap - with none, the request
        # stays byte-identical to the plain status upload. The legacy path
        # is status-only as before.
        attachment_parts: list[tuple[str, bytes, str]] = []
        upload_comment = comment
        if self.use_playwright_endpoint:
            parts_by_case = self._collect_attachment_parts(case_keys)
            attachment_parts = list(parts_by_case.values())
            if attachment_parts:
                upload_comment = '\n'.join(
                    [comment, *self._timing_comment_lines(list(parts_by_case))])
                log.info(f'QA Touch: attaching {len(attachment_parts)} '
                         f'failure screenshot(s) to the upload.')

        # Both paths use the case-code endpoint. It is the only one measured
        # to actually apply a status, and its response names the case it
        # changed. QATouchClient.update_playwright_results has never existed -
        # the playwright branch used to raise AttributeError on every attempt.
        def _plain_upload():
            # The exact pre-attachment request - the degrade target for
            # both attachment failure shapes below.
            client.update_test_run_results_by_code(
                project_key=self.project_key,
                test_run_key=self.run_key,
                results=plain_results,
                comments=comment,
            )

        # True when the attempt that succeeded carried the multipart body.
        # The verify step needs it: this API accepts requests it ignores,
        # so a multipart "success" is not proof the statuses applied.
        sent_attachments = False

        def _do_upload():
            nonlocal sent_attachments
            if attachment_parts:
                try:
                    client.update_test_run_results_by_code(
                        project_key=self.project_key,
                        test_run_key=self.run_key,
                        results=plain_results,
                        comments=upload_comment,
                        attachments=attachment_parts,
                    )
                    sent_attachments = True
                    return
                except Exception as e:  # statuses must never be hostage to a screenshot
                    log.warning(f'QA Touch: upload with attachments failed '
                                f'({e}). Sending the statuses without '
                                f'attachments.')
            sent_attachments = False
            _plain_upload()

        success = self._upload_with_retry(_do_upload)
        failure_reason = f'Upload failed after {MAX_RETRIES} attempts'
        if success:
            success = self._verify_upload(client, results_payload)
            if not success and sent_attachments:
                # The raising fallback in _do_upload cannot see this API's
                # other failure shape: a multipart request answered with
                # success while the statuses never applied (the accepted-
                # but-ignored class, measured 2026-08-17). Degrade the same
                # way - re-send the plain pre-attachment request and read
                # back once more before conceding to the failure log.
                log.warning('QA Touch: the upload with attachments was '
                            'accepted but the read-back shows the statuses '
                            'did not land. Re-sending the statuses without '
                            'attachments.')
                if self._upload_with_retry(_plain_upload):
                    success = self._verify_upload(client, results_payload)
            if not success:
                failure_reason = ('Upload reported success but the read-back '
                                  'shows the statuses did not land')

        if success:
            self.stats['uploaded'] = len(results_payload)
            log.info(f'QA Touch: Successfully uploaded {len(results_payload)} results.')
        else:
            self.stats['errors'] += 1
            # Save to failure log for later retry
            self._save_failed_results(results_payload, comment, failure_reason)

    def pytest_terminal_summary(self, terminalreporter: Any, exitstatus: int) -> None:
        """Print QA Touch upload summary in the terminal."""
        terminalreporter.write_sep('=', 'QA Touch Result Upload')
        terminalreporter.write_line(f'  Run key:    {self.run_key}')
        terminalreporter.write_line(f'  Project:    {self.project_key}')
        path_used = getattr(self, '_upload_path_used', '(none)')
        terminalreporter.write_line(f'  Upload path: {path_used}')
        terminalreporter.write_line(
            f'  Endpoint:   {"playwright (rich)" if self.use_playwright_endpoint else "legacy (status-only)"}')
        terminalreporter.write_line(f'  Mapped:     {self.stats["mapped"]} results')
        terminalreporter.write_line(f'  Unmapped:   {self.stats["unmapped"]} tests (no case mapping)')
        terminalreporter.write_line(f'  Uploaded:   {self.stats["uploaded"]} results')
        if self.stats.get('screenshots'):
            terminalreporter.write_line(
                f'  Screenshots: {self.stats["screenshots"]} captured on failure')
        if self.stats['retries']:
            terminalreporter.write_line(f'  Retries:    {self.stats["retries"]}')
        if self.stats['errors']:
            terminalreporter.write_line(f'  Errors:     {self.stats["errors"]}')
        if self.stats['saved_for_retry']:
            terminalreporter.write_line(
                f'  Saved:      {self.stats["saved_for_retry"]} results in '
                f'{FAILURE_LOG_PATH} (retry later)')

        # Breakdown by status
        passed = sum(1 for s in self.results.values() if s == STATUS_PASSED)
        failed = sum(1 for s in self.results.values() if s == STATUS_FAILED)
        blocked = sum(1 for s in self.results.values() if s == STATUS_BLOCKED)
        terminalreporter.write_line(f'  Passed:     {passed}')
        terminalreporter.write_line(f'  Failed:     {failed}')
        terminalreporter.write_line(f'  Blocked:    {blocked}')
