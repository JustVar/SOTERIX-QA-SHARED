"""What a test run produced, and where those results can be sent.

The node id is the identity. Everything downstream already agrees on that -
`last_run_results.json` is written per node id, the Jira bug filer keys off
it, and a test with no QA Touch case still has one. A `case_key` does not
work as the primary key for three measured reasons:

  * It is not unique. Across the UI mapping, 42 case keys are claimed by
    more than one node id (38 after collapsing pytest parametrization). Keyed
    by case key, the second test's duration and screenshot overwrite the
    first's, and only the worse status survives - silently.
  * It is optional. An unmapped test has no case key at all, so keying by
    it means the primary record cannot hold the very failures most likely
    to need a bug filed.
  * It is QA Touch's word for it. A JUnit writer, a Jira filer or a
    dashboard has no use for it, and should not have to learn it to read a
    result.

So the reporter records `RunResult` per node id and hands the whole list to
each registered sink at session end. Collapsing to case keys is one sink's
private business, not the shape everything else has to live with.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Worse outcomes win when the same node id reports twice (reruns).
OUTCOME_RANK = {'passed': 0, 'skipped': 1, 'failed': 2}


def worse_outcome(new: str, existing: str | None) -> bool:
    """True when `new` should replace `existing`."""
    return OUTCOME_RANK.get(new, -1) > OUTCOME_RANK.get(existing or '', -1)


@dataclass
class RunResult:
    """One test's outcome, as the session saw it.

    `case_key` and `project_key` are resolved at record time because that is
    when the pytest item - and therefore its markers - is still available.
    They are carried, not interpreted: only the QA Touch sink cares.
    """

    nodeid: str
    outcome: str
    duration: float | None = None
    case_key: str | None = None
    project_key: str | None = None
    screenshot: bytes | None = field(default=None, repr=False)

    @property
    def base_nodeid(self) -> str:
        """The node id without its parametrization suffix."""
        return self.nodeid.split('[')[0]


def merge_reruns(existing: RunResult, new: RunResult) -> RunResult:
    """Keep the worse of two reports for the same node id.

    A rerun (pytest-rerunfailures) reports the same node id twice. The
    status already took the worse of the two; the duration and the
    screenshot used to take the LAST, so a case could upload "failed" next
    to the passing rerun's timing. Everything now travels with the outcome
    it belongs to.
    """
    if worse_outcome(new.outcome, existing.outcome):
        return new
    return existing


@dataclass
class RunContext:
    """What a sink needs to know about the SESSION, not about one test.

    A per-test record cannot answer "how long did the suite take" (the sum of
    test durations is not wall clock), "which branch was this", or "what did
    the invocation ask for". Any run-level destination - a ledger, a JUnit
    file, a dashboard feed - needs those, so they are gathered once and
    handed to every sink rather than each one re-deriving them.
    """

    started: datetime
    ended: datetime
    env: str = 'unknown'
    target: tuple[str, ...] = ()
    exitstatus: int | None = None
    run_key: str | None = None
    project_key: str | None = None
    branch: str | None = None
    commit: str | None = None

    @property
    def duration_s(self) -> float:
        return round((self.ended - self.started).total_seconds(), 1)

    @property
    def armed(self) -> bool:
        """True when this session was uploading to QA Touch."""
        return self.run_key is not None


def git_info(root: Path | str) -> tuple[str | None, str | None]:
    """(branch, short commit) for a repo, or (None, None).

    Best effort and never raises: a checkout can be a tarball, a detached
    head, or have no git at all, and none of those is a reason to fail a
    test run. Short timeout because a hung git would hold the session open
    at the one moment a developer is waiting to see results.
    """
    def _run(*args: str) -> str | None:
        try:
            out = subprocess.run(
                ['git', *args], cwd=str(root), capture_output=True,
                text=True, timeout=5, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        value = out.stdout.strip()
        return value if out.returncode == 0 and value else None

    return _run('rev-parse', '--abbrev-ref', 'HEAD'), _run('rev-parse', '--short', 'HEAD')


class ResultSink:
    """Somewhere a run's results can go.

    Subclasses override `finish`. `record` is optional and exists for a sink
    that wants results as they happen rather than in a batch.

    A sink must not raise. It runs inside `pytest_sessionfinish`, where an
    exception is reported against the session rather than against the sink,
    and one broken destination must not cost the run its other destinations.
    The reporter isolates each sink for that reason, but a sink that handles
    its own failures can say something useful about them in `summary`.
    """

    name = 'sink'

    # A sink that cannot do its job without case keys sets this, so a
    # missing mapping policy can be reported as the failure it actually is
    # for THIS session rather than in the same words either way.
    needs_case_keys = False
    # A sink that wants a PNG captured on failure sets this. Capturing costs
    # a browser round trip, so nothing captures speculatively.
    wants_screenshots = False

    def record(self, result: RunResult) -> None:
        """Called as each test finishes. Default: wait for finish()."""

    def finish(self, results: list[RunResult], context: RunContext) -> None:
        """Called once at session end with every result and the run's facts."""
        raise NotImplementedError

    def summary(self) -> list[str]:
        """Lines for the terminal summary. Empty means say nothing."""
        return []


class RunResultsFileSink(ResultSink):
    """Writes the per-node-id record other tools in the repo read.

    Registered unconditionally, unlike the upload. Before the sink split
    this file was written only when QA Touch upload was armed, because the
    reporter did not exist otherwise - so an ordinary `pytest` run left the
    flaky history and the run cross-check with nothing to read. Nothing
    about a local record needs a QA Touch run key, so it no longer waits
    for one.

    `armed` records whether the session was also uploading. A reader that
    finds a run key of None can then tell "this run never intended to
    upload" from "this run meant to and could not", which the file could
    not express before.
    """

    name = 'run results file'

    def __init__(self, path: Path):
        self.path = path
        self.written = 0
        self.error: str | None = None
        self.skipped_empty = False

    def finish(self, results: list[RunResult], context: RunContext) -> None:
        # A session that recorded nothing must not replace a session that
        # did. Measured across 21 invocations: -x, --maxfail, a -k or -m
        # that matches nothing, --deselect, a collection error, a mistyped
        # node id, a nonexistent path, --fixtures and Ctrl-C all reached
        # this point with an empty list and blanked a real record. Every
        # consumer then read the empty file as "a run with no failures"
        # rather than "no run", because they check only that it exists.
        #
        # This is the general guard. The dry-pass option list in the plugin
        # avoids pointless work, but THIS is what makes the record safe:
        # an option list can only ever cover the flags somebody thought of.
        if not results:
            self.written = 0
            self.skipped_empty = True
            return
        self.skipped_empty = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                'timestamp': datetime.now(UTC).isoformat(),
                'run_key': context.run_key,
                'project_key': context.project_key,
                'env': context.env,
                'armed': context.armed,
                # pytest's exit status. 0 is a complete pass, 1 some tests
                # failed, 2 interrupted, 5 nothing was collected. Without
                # it, a run stopped by -x or Ctrl-C is indistinguishable on
                # disk from a smaller suite that finished cleanly.
                'exit': context.exitstatus,
                'results': [
                    {
                        'nodeid': r.nodeid,
                        'outcome': r.outcome,
                        'duration': r.duration,
                        'case_key': r.case_key,
                    }
                    for r in results
                ],
            }
            with open(self.path, 'w') as f:
                json.dump(payload, f, indent=2)
            self.written = len(results)
        except Exception as exc:  # a local write must never fail a test run
            self.error = str(exc)

    def summary(self) -> list[str]:
        if self.skipped_empty:
            return ['  Run record: unchanged (this session recorded nothing)']
        if self.error:
            return [f'  Run record: NOT written ({self.error})']
        return [f'  Run record: {self.written} results -> {self.path}']


class RunLedgerSink(ResultSink):
    """Appends one line per run to a history file that IS tracked in git.

    The reports directory holds 4.4 GB after a week: traces, videos and
    screenshots, all regenerable by re-running, none of it belonging in a
    repository. But a run leaves one thing worth keeping - the fact that it
    happened, when, on which commit, and what failed. That is about 300
    bytes, and it answers the questions the evidence cannot: when did this
    test start failing, is the suite getting slower, what did the suite look
    like before the release.

    **Only armed runs are ledgered.** A run that uploaded to QA Touch is one
    somebody meant; an exploratory `pytest -k something` is not, and 71 of
    those in a week would bury the runs that matter. The distinction already
    exists, so nobody has to remember a flag.

    One file per month keeps concurrent appends apart. When two people do
    collide, the conflict is two adjacent lines: keep both, in timestamp
    order. Never rewrite an existing line - the ledger is append-only, and a
    row that changes is a row that cannot be trusted.
    """

    name = 'run ledger'

    def __init__(self, directory: Path, max_failures: int = 25):
        self.directory = directory
        # A catastrophic run (the environment is down, 600 tests fail) would
        # otherwise write a 60 KB line and drown the file it lives in. The
        # count is always exact; only the list of names is capped.
        self.max_failures = max_failures
        self.path: Path | None = None
        self.written = False
        self.error: str | None = None

    def row(self, results: list[RunResult], context: RunContext) -> dict[str, Any]:
        counts: dict[str, int] = {}
        failures: list[str] = []
        for r in results:
            counts[r.outcome] = counts.get(r.outcome, 0) + 1
            if r.outcome == 'failed':
                failures.append(r.nodeid)
        failures.sort()
        row = {
            'at': context.started.isoformat(timespec='seconds'),
            'env': context.env,
            'branch': context.branch,
            'commit': context.commit,
            'run_key': context.run_key,
            'project_key': context.project_key,
            'target': list(context.target),
            'dur_s': context.duration_s,
            'exit': context.exitstatus,
            'counts': counts,
            'failed': failures[:self.max_failures],
        }
        if len(failures) > self.max_failures:
            row['failed_truncated'] = len(failures) - self.max_failures
        return row

    def finish(self, results: list[RunResult], context: RunContext) -> None:
        if not context.armed:
            return
        # An armed session that ran nothing leaves a permanent row saying
        # counts={} failed=[], which no later reader can tell from a real
        # run where everything was deselected. The ledger is append-only,
        # so a wrong row cannot be taken back.
        if not results:
            return
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            self.path = self.directory / f'{context.started:%Y-%m}.jsonl'
            line = json.dumps(self.row(results, context), sort_keys=True)
            with open(self.path, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
            self.written = True
        except Exception as exc:  # history is nice to have, never worth a run
            self.error = str(exc)

    def summary(self) -> list[str]:
        if self.error:
            return [f'  Run ledger: NOT written ({self.error})']
        if not self.written:
            return []
        return [f'  Run ledger: appended to {self.path}']


def collapse_to_cases(results: list[RunResult],
                      status_for: dict[str, int]) -> dict[str, Any]:
    """Fold node-id results into the per-case view QA Touch needs.

    Where several node ids share a case key - 42 do in the UI mapping - the
    worst status wins, which is the same rule reruns already follow. The
    duration and screenshot come from whichever of them reported worst,
    rather than from whichever happened to run last: a screenshot that does
    not belong to the uploaded status is worse than no screenshot.

    Returns statuses, durations, screenshots and projects keyed by case key,
    plus the node ids each case drew from, so a caller can say which tests a
    single uploaded status actually covers.
    """
    statuses: dict[str, int] = {}
    durations: dict[str, float] = {}
    screenshots: dict[str, bytes] = {}
    projects: dict[str, str] = {}
    nodeids: dict[str, list[str]] = {}
    best_outcome: dict[str, str] = {}

    for r in results:
        if not r.case_key:
            continue
        status = status_for.get(r.outcome)
        if status is None:
            continue
        key = r.case_key
        nodeids.setdefault(key, []).append(r.nodeid)

        if key not in statuses:
            statuses[key] = status
            wins = True
        else:
            wins = worse_outcome(r.outcome, best_outcome.get(key))
            statuses[key] = max(statuses[key], status)

        if wins:
            best_outcome[key] = r.outcome
            if r.duration is not None:
                durations[key] = r.duration
            if r.screenshot:
                screenshots[key] = r.screenshot
        if r.project_key:
            projects[key] = r.project_key

    return {
        'statuses': statuses,
        'durations': durations,
        'screenshots': screenshots,
        'projects': projects,
        'nodeids': nodeids,
    }
