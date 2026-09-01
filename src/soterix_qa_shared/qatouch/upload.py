"""The QA Touch upload sink.

Everything about talking to QA Touch lives here, so the reporter stays a
recorder: it collects one `RunResult` per node id and hands the list over.
Folding those results into per-case statuses is this sink's private
business, because the case key is QA Touch's word for a test and nothing
else in the pipeline needs it.

Three measured traps this file exists to avoid, all of the same shape - the
API answers success for work it did not do:

  1. The results endpoint identifies a case by its CODE (TR0263). Send the
     case key or the per-run result key and it returns {"success": true}
     and changes nothing (measured 2026-08-17).
  2. A multipart request carrying attachments can be accepted and ignored
     the same way, so a 200 is not proof the statuses applied.
  3. An empty payload "verifies" trivially, which would let a run with no
     resolvable case codes report success having uploaded nothing.

So every upload is read back, and an upload that resolved no case codes is
a failure written to the retry log, never an empty success.
"""
from __future__ import annotations

import base64
import logging
import time
from datetime import UTC, datetime
from typing import Any

from soterix_qa_shared.qatouch.results import (
    ResultSink,
    RunContext,
    RunResult,
    collapse_to_cases,
)

log = logging.getLogger('qatouch_reporter')

# Retry configuration
MAX_RETRIES = 3
RETRY_BASE_DELAY = 2  # seconds, doubles each attempt

# Per-file limit for result attachments. The code endpoint accepts multipart
# file[] parts (png/pdf/jpg/csv/log) up to 2 MB each. Enforced before
# anything is sent: an oversized file is skipped whole, never truncated,
# because a cut PNG uploads fine and opens broken - worse than absent.
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

DEFAULT_PROJECT_KEY = 'JlW1'


def format_duration(seconds: float | None) -> str:
    """Format a pytest duration for QA Touch's `execution_time` field.

    Matches what the vendor playwright reporter emits ("1.2s", "450ms") so
    values are comparable in the QA Touch UI.
    """
    if seconds is None or seconds < 0:
        return ''
    if seconds < 1.0:
        return f'{round(seconds * 1000)}ms'
    return f'{seconds:.1f}s'


class QATouchUploadSink(ResultSink):
    """Uploads a run's results to a QA Touch test run."""

    name = 'qatouch upload'
    needs_case_keys = True

    def __init__(self, run_key: str, project_key: str = DEFAULT_PROJECT_KEY,
                 use_playwright_endpoint: bool = False,
                 capture_screenshots: bool = False,
                 failure_log_path: Any = None):
        self.run_key = run_key
        self.project_key = project_key
        self.use_playwright_endpoint = use_playwright_endpoint
        self.wants_screenshots = capture_screenshots
        self.failure_log_path = failure_log_path

        # Filled during finish().
        self.statuses: dict[str, int] = {}
        self.durations: dict[str, float] = {}
        self.screenshots: dict[str, bytes] = {}
        self.case_projects: dict[str, str] = {}
        self.case_nodeids: dict[str, list[str]] = {}
        self.case_to_code: dict[str, str] = {}

        self.stats = {'uploaded': 0, 'errors': 0, 'retries': 0,
                      'saved_for_retry': 0}
        self.upload_path_used = '(none)'
        # Filled by the read-back. None means no verification ran, which
        # is not the same as a run with no cases, so summary() stays quiet.
        self.run_case_count: int | None = None
        self.uncovered_cases: list[str] = []
        # True when the read-back could not run at all (network), so the
        # upload is unverified rather than confirmed. Saved for retry.
        self.verify_unavailable: bool = False

    # -- resolution ---------------------------------------------------------

    def _build_case_code_mapping(self, client) -> None:
        """Build case_key -> case CODE (e.g. "TR0263") for this run.

        Trap 1: the results API identifies a case by its code, not by the
        case key and not by the per-result run key. Sending either returns
        success and changes nothing, so a broken upload is indistinguishable
        from a working one. This mapping is what makes an upload land.

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
                    self.case_to_code[case_id] = code
            if self.case_to_code:
                log.info(f'QA Touch: Resolved case codes for '
                         f'{len(self.case_to_code)} cases in run {self.run_key}')
            else:
                log.error('QA Touch: Could not resolve any case codes for this run. '
                          'Uploads would silently do nothing, so they will be skipped.')
        except Exception as e:
            log.error(f'QA Touch: Could not build the case-code mapping: {e}. '
                      f'Uploads would silently do nothing, so they will be skipped.')

    def _group_by_project(self) -> dict[str, list[str]]:
        """Group recorded case keys by their resolved project key."""
        groups: dict[str, list[str]] = {}
        for case_key in self.statuses:
            pk = self.case_projects.get(case_key, self.project_key)
            groups.setdefault(pk, []).append(case_key)
        return groups

    # -- payload ------------------------------------------------------------

    def _build_payload(self, case_keys: list[str]) -> list[dict]:
        """Build the results payload for a subset of case keys."""
        results_payload = []
        translated = 0
        for case_key in case_keys:
            status = self.statuses[case_key]
            code = self.case_to_code.get(case_key)
            if not code:
                # Sending the case key instead would be accepted and ignored.
                log.warning(f'QA Touch: no case code for {case_key} in run '
                            f'{self.run_key} - skipping it rather than sending '
                            f'an identifier the API ignores.')
                continue
            translated += 1
            entry = {'case': code, 'status': status}
            if self.use_playwright_endpoint:
                duration = self.durations.get(case_key)
                entry['execution_time'] = format_duration(duration) if duration is not None else ''
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
            log.info(f'QA Touch: Translated {translated} case_keys to case codes.')
        return results_payload

    def _collect_attachment_parts(self, case_keys: list[str]) -> dict[str, tuple[str, bytes, str]]:
        """Build multipart file parts for failed cases with captured screenshots.

        Returns {case_key: (filename, content, content_type)}. Keyed by case
        key so the caller knows which cases actually ride - the timing
        comment covers exactly those. Files are named by case CODE, because
        the run UI shows codes and a reader can then match a file to its
        case without the mapping.

        A file over MAX_ATTACHMENT_BYTES is skipped whole with a log line.
        A case with no resolved code is skipped too - _build_payload already
        dropped its result, so there is nothing for a file to hang on.
        """
        parts: dict[str, tuple[str, bytes, str]] = {}
        for case_key in case_keys:
            png = self.screenshots.get(case_key)
            if not png:
                continue
            if self.statuses.get(case_key) != STATUS_FAILED:
                # Screenshots come from a failed call phase; if a rerun later
                # improved the recorded status, the picture no longer matches
                # the result being uploaded.
                continue
            code = self.case_to_code.get(case_key)
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

        The comments field is the only documented place for timing on the
        code endpoint - the result JSON carries case and status alone. The
        client sends comments in the URL query string, so the caller passes
        only the attached cases: a line for every uploaded case would grow
        the URL with run size and risk rejection on exactly the large runs
        attachments target.
        """
        lines: list[str] = []
        for case_key in case_keys:
            duration = self.durations.get(case_key)
            code = self.case_to_code.get(case_key)
            if duration is None or not code:
                continue
            lines.append(f'{code} execution: {format_duration(duration)}')
        return lines

    # -- transport ----------------------------------------------------------

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
                    log.warning(f'QA Touch: Upload attempt {attempt}/{MAX_RETRIES} '
                                f'failed: {e}. Retrying in {delay}s...')
                    self.stats['retries'] += 1
                    time.sleep(delay)
                else:
                    log.error(f'QA Touch: Upload failed after {MAX_RETRIES} '
                              f'attempts: {last_error}')
        return False

    def _verify_upload(self, client, results_payload: list) -> bool:
        """Read the run back and confirm the statuses actually changed.

        Traps 1 and 2: QA Touch reports success for uploads it silently
        discards, so an upload is not done until the run agrees.
        """
        self.verify_unavailable = False
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
                # The read-back could not run. The POST may have landed or
                # been silently discarded, which is exactly the case verify
                # exists to catch, so booking it success would re-open the
                # accept-and-ignore trap through the back door. Treat it as
                # unverified and save for retry: a replay re-writes the same
                # statuses and reads back later, so at worst it re-confirms an
                # upload that did land.
                log.warning(f'QA Touch: could not verify the upload ({e}). '
                            f'Saving for retry rather than assuming it landed.')
                self.verify_unavailable = True
                return False

        problems = report['mismatched'] + [f'{c}: not in run' for c in report['missing']]
        if problems:
            log.error(f'QA Touch: {len(problems)} result(s) did not land. '
                      f'First few: {problems[:5]}')
            return False
        # Kept for summary(). "Did my write land" and "is the run
        # finished" are different questions, and only the first was ever
        # answered out loud.
        self.run_case_count = report.get('in_run')
        self.uncovered_cases = list(report.get('uncovered') or ())
        log.info(f'QA Touch: verified {len(report["landed"])} result(s) in the run.')
        return True

    def _save_failed_results(self, results_payload: list, comment: str, error: str) -> None:
        """Append failed upload results to the retry log.

        The entry shape is a contract with the retry CLI and each repo's
        validate_run - see qatouch.artifacts.FAILURE_ENTRY_FIELDS.
        """
        import json
        path = self.failure_log_path
        if path is None:
            log.error('QA Touch: no failure log path configured - '
                      f'{len(results_payload)} result(s) could not be saved for retry.')
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)

            existing = []
            if path.exists():
                try:
                    with open(path) as f:
                        existing = json.load(f)
                except (json.JSONDecodeError, ValueError):
                    existing = []

            existing.append({
                'timestamp': datetime.now(UTC).isoformat(),
                'project_key': self.project_key,
                'run_key': self.run_key,
                'result_count': len(results_payload),
                'results': results_payload,
                'comment': comment,
                'error': str(error),
            })

            with open(path, 'w') as f:
                json.dump(existing, f, indent=2)

            self.stats['saved_for_retry'] = len(results_payload)
            log.info(f'QA Touch: Saved {len(results_payload)} results to '
                     f'{path} for later retry')
        except Exception as save_err:
            log.error(f'QA Touch: Failed to save results for retry: {save_err}')

    # -- the sink interface -------------------------------------------------

    def finish(self, results: list[RunResult], context: RunContext | None = None) -> None:
        # `context` is accepted and unused: this sink was given its run key at
        # construction, because it needs one to exist at all. The parameter
        # stays because a sink author reads this file as the worked example,
        # and a signature that disagrees with ResultSink teaches the wrong
        # shape. The default is for direct callers in tests only.
        folded = collapse_to_cases(results, PYTEST_TO_QT)
        self.statuses = folded['statuses']
        self.durations = folded['durations']
        self.screenshots = folded['screenshots']
        self.case_projects = folded['projects']
        self.case_nodeids = folded['nodeids']

        if not self.statuses:
            log.info('QA Touch: No mapped results to upload.')
            return

        # Any project other than the default needs its own run key, and we
        # have exactly one. Say which cases are being dropped and why.
        project_groups = self._group_by_project()
        cross_project = [pk for pk in project_groups if pk != self.project_key]
        if cross_project:
            log.error(
                f'QA Touch: Cases target non-default projects {cross_project} '
                f'but no per-project run key is configured. Default project: '
                f'{self.project_key}, run: {self.run_key}. Skipping those cases.')
            for pk in cross_project:
                self.stats['errors'] += 1
                skipped = project_groups.pop(pk)
                log.error(f'QA Touch: Skipped {len(skipped)} case(s) for '
                          f'project {pk}: {skipped}')

        case_keys = project_groups.get(self.project_key, [])
        if not case_keys:
            log.info('QA Touch: No uploadable mapped results after project filtering.')
            return

        timestamp = datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')
        comment = f'Automated upload from Playwright run at {timestamp}'

        # From the SUBMODULE, not the package namespace. Measured: with
        # `from soterix_qa_shared.qatouch import QATouchClient`, a consumer's
        # shim that aliases tools/qatouch_client.py to this submodule (the
        # form docs/RULES.md prescribes) passes the identity guard while its
        # monkeypatch never reaches here, so a test that believed it had a
        # mock builds a REAL client against live credentials. Importing from
        # the submodule makes the aliased path and the read path one object.
        try:
            from soterix_qa_shared.qatouch.client import QATouchClient
            client = QATouchClient()
        except Exception as e:
            # The commonest cause is a missing API key, e.g. mid-rotation.
            # Every other failure path in finish() saves for retry; this one
            # used to drop the run's results on the floor. Save the case-key
            # payload (the retry CLI resolves keys to codes) so a replay lands
            # them once the key is back.
            log.error(f'QA Touch: Failed to initialize client: {e}. '
                      f'Saving {len(case_keys)} result(s) for retry.')
            self.stats['errors'] += 1
            self._save_failed_results(
                [{'case': k, 'status': self.statuses[k]} for k in case_keys],
                comment, f'Client init failed ({e}) - saved for retry')
            return

        try:
            self._build_case_code_mapping(client)
        except Exception as e:
            log.warning(f'QA Touch: case-code mapping failed (non-fatal): {e}')

        results_payload = self._build_payload(case_keys)

        if not results_payload:
            # Trap 3. Sending case keys instead would be accepted and
            # ignored, and an empty POST would "verify" trivially - either
            # way the summary would claim success while nothing was written.
            # That is the silent-no-op class this sink exists to prevent, so
            # it is an upload FAILURE saved for retry, not an empty success.
            self.stats['errors'] += 1
            self._save_failed_results(
                [{'case': k, 'status': self.statuses[k]} for k in case_keys],
                comment,
                'No case codes resolved for this run - nothing was sent. '
                'The retry CLI resolves the keys and replays.')
            log.error(f'QA Touch: could not resolve ANY of the {len(case_keys)} '
                      f'mapped case(s) to case codes for run {self.run_key}. '
                      f'NOTHING was uploaded. Saved to the failure log.')
            self.upload_path_used = 'failed (no case codes)'
            return

        endpoint_label = 'playwright' if self.use_playwright_endpoint else 'legacy'
        log.info(f'QA Touch: Uploading {len(results_payload)} results to run '
                 f'{self.run_key} via {endpoint_label} endpoint...')
        self.upload_path_used = endpoint_label

        plain_results = [{'case': r['case'], 'status': r['status']}
                         for r in results_payload]

        # The playwright path also sends what the run captured: failure
        # screenshots as multipart file[] parts, and execution times for the
        # ATTACHED cases in the comment. Both ride only when a screenshot
        # survived the cap - with none, the request stays byte-identical to
        # the plain status upload. The legacy path is status-only as before.
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

        def _plain_upload():
            # The exact pre-attachment request - the degrade target for both
            # attachment failure shapes below.
            client.update_test_run_results_by_code(
                project_key=self.project_key,
                test_run_key=self.run_key,
                results=plain_results,
                comments=comment,
            )

        # True when the attempt that succeeded carried the multipart body.
        # The verify step needs it: trap 2 means a multipart "success" is
        # not proof the statuses applied.
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
                # The raising fallback in _do_upload cannot see trap 2: a
                # multipart request answered with success while the statuses
                # never applied. Degrade the same way - re-send the plain
                # request and read back once more before conceding.
                log.warning('QA Touch: the upload with attachments was '
                            'accepted but the read-back shows the statuses '
                            'did not land. Re-sending the statuses without '
                            'attachments.')
                if self._upload_with_retry(_plain_upload):
                    success = self._verify_upload(client, results_payload)
            if not success:
                failure_reason = (
                    'Upload posted but the read-back could not run, so it is '
                    'unverified - saved for retry'
                    if getattr(self, 'verify_unavailable', False)
                    else 'Upload reported success but the read-back shows the '
                         'statuses did not land')

        if success:
            self.stats['uploaded'] = len(results_payload)
            log.info(f'QA Touch: Successfully uploaded {len(results_payload)} results.')
        else:
            self.stats['errors'] += 1
            self._save_failed_results(results_payload, comment, failure_reason)

    def summary(self) -> list[str]:
        lines = [
            f'  Run key:    {self.run_key}',
            f'  Project:    {self.project_key}',
            f'  Upload path: {self.upload_path_used}',
            '  Endpoint:   ' + ('playwright (rich)' if self.use_playwright_endpoint
                                else 'legacy (status-only)'),
            f'  Uploaded:   {self.stats["uploaded"]} results',
        ]
        # Where several tests share one case key, one uploaded status stands
        # for all of them. Say so - it is otherwise invisible, and it is the
        # measured shape of this mapping (42 such keys in the UI repo).
        shared = {k: n for k, n in self.case_nodeids.items() if len(n) > 1}
        if shared:
            lines.append(f'  Shared:     {len(shared)} case(s) cover more than '
                         f'one test; the worst status won')
        if self.run_case_count:
            set_here = self.run_case_count - len(self.uncovered_cases)
            lines.append(f'  Run coverage: {self.run_case_count} case(s) in '
                         f'{self.run_key}; this run set {set_here}')
            if self.uncovered_cases:
                shown = ', '.join(self.uncovered_cases[:5])
                more = (f' (+{len(self.uncovered_cases) - 5} more)'
                        if len(self.uncovered_cases) > 5 else '')
                lines.append(f'  Not covered: {shown}{more} - no selected '
                             f'test maps to these')
        if self.stats['retries']:
            lines.append(f'  Retries:    {self.stats["retries"]}')
        if self.stats['errors']:
            lines.append(f'  Errors:     {self.stats["errors"]}')
        if self.stats['saved_for_retry']:
            lines.append(f'  Saved:      {self.stats["saved_for_retry"]} results in '
                         f'{self.failure_log_path} (retry later)')
        passed = sum(1 for s in self.statuses.values() if s == STATUS_PASSED)
        failed = sum(1 for s in self.statuses.values() if s == STATUS_FAILED)
        blocked = sum(1 for s in self.statuses.values() if s == STATUS_BLOCKED)
        lines += [f'  Passed:     {passed}',
                  f'  Failed:     {failed}',
                  f'  Blocked:    {blocked}']
        return lines
