"""Replay failed QA Touch uploads from the failure log.

When the reporter cannot upload results after its retries, it saves them to
`logs/upload_failures.json`. This replays them.

Usage (through the consumer repo's shim):
  python tools/retry_uploads.py           # retry all pending
  python tools/retry_uploads.py --dry-run # show what would be retried
  python tools/retry_uploads.py --clear   # clear failure log without retrying

Every retry is read back from the run afterwards. QA Touch answers success
for uploads it silently discards, so an entry only leaves the log when the
run actually shows the status that was sent.

Entries whose case identifiers are KEYS rather than codes (TR0263) are
resolved first: the reporter saves keys when the run was unreachable at
session end, and the run's own rows carry key -> code once it answers. What
still cannot be resolved moves to `logs/upload_failures_unreplayable.json`
rather than silently "succeeding" - the results API ignores case keys and
reports success anyway.

This module has a CLI, so its consumer shim needs the GUARDED form. A plain
alias shim swaps `sys.modules['__main__']` and exits 0 having run nothing -
the same silent no-op as `python -m` on an aliased module. The shim is also
where `.env` gets loaded: the client reads credentials from `os.environ` and
a package has no business reaching into a repo's dotenv file.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from soterix_qa_shared.qatouch.layout import paths_for
from soterix_qa_shared.qatouch.results import write_json_atomic

# Set by configure(); read at call time so tests can patch them.
FAILURE_LOG_PATH: Path | None = None
UNREPLAYABLE_PATH: Path | None = None

CASE_CODE_RE = re.compile(r'^TR\d+$')

PASS = '\033[92mPASS\033[0m'
FAIL = '\033[91mFAIL\033[0m'
WARN = '\033[93mWARN\033[0m'

DEFAULT_PROJECT_KEY = 'JlW1'


def configure(root: Path | str) -> None:
    """Point the log paths at a repo root. Called by the consumer's shim."""
    global FAILURE_LOG_PATH, UNREPLAYABLE_PATH
    paths = paths_for(root)
    FAILURE_LOG_PATH = paths['failure_log']
    UNREPLAYABLE_PATH = paths['unreplayable_log']


def _replayable(entry: dict) -> bool:
    """True when the entry HAS results and every one names a case code.

    The emptiness check is not defensive tidying. `all([])` is True, so an
    entry with no results counted as replayable: measured, the CLI POSTed an
    empty payload, read back zero results, found no problems, printed "All 1
    retries succeeded and verified" and deleted the log. That is trap 3 -
    an empty payload verifying trivially - which upload.py exists to close
    and which was wide open here.
    """
    rows = entry.get('results') or []
    return bool(rows) and all(
        CASE_CODE_RE.match(str(r.get('case', ''))) for r in rows)


def translate_keys_to_codes(entry: dict, run_rows: list[dict]) -> tuple[bool, list[dict]]:
    """Rewrite an entry's case KEYS to case CODES using the run's own rows.

    The reporter saves case keys when it cannot reach the run to resolve
    codes (e.g. a 429 at session end). The run's result rows carry both
    (case_id = key, code = TR0263), so once the run is reachable the entry
    becomes replayable. Returns (all_translated, results_with_codes).
    """
    by_key = {r.get('case_id'): r.get('code') for r in run_rows
              if r.get('case_id') and r.get('code')}
    out = []
    complete = True
    for r in entry.get('results', []):
        case = str(r.get('case', ''))
        if CASE_CODE_RE.match(case):
            out.append({'case': case, 'status': r['status']})
        elif case in by_key:
            out.append({'case': by_key[case], 'status': r['status']})
        else:
            complete = False
    return complete, out


def _load_failures() -> tuple[list | None, int]:
    """Read the failure log. Returns (entries, exit_code); entries is None
    when the caller should stop."""
    if FAILURE_LOG_PATH is None:
        print('No repo root was configured, so there is no failure log to '
              'read. The consumer shim calls retry.configure(ROOT).')
        return None, 2
    if not FAILURE_LOG_PATH.exists():
        print('No failure log found - nothing to retry.')
        return None, 0
    try:
        with open(FAILURE_LOG_PATH, encoding='utf-8') as f:
            failures = json.load(f)
    except (json.JSONDecodeError, ValueError):
        print(f'Failure log is malformed: {FAILURE_LOG_PATH}')
        return None, 2
    if not failures:
        print('Failure log is empty - nothing to retry.')
        return None, 0
    return failures, 0


def _resolve_stale(stale: list[dict], replayable: list[dict],
                   transient: list[dict]) -> list[dict]:
    """Turn key-based entries into code-based ones where the run allows.

    Returns the entries that are GENUINELY unreplayable (case keys the run
    does not contain). A network failure while resolving is not that: those
    entries go to `transient`, which the caller keeps in the retry queue for
    a later run rather than parking them as permanently dead. Before this, a
    single flaky read during resolution parked a recoverable upload forever.
    """
    try:
        # From the submodule, so a consumer shim aliased to it stays
        # patchable. See the note in upload.py.
        from soterix_qa_shared.qatouch.client import QATouchClient
        client = QATouchClient()
    except Exception as e:
        # The client itself is unavailable (network, missing key). Every
        # stale entry is transient, not unreplayable.
        print(f'  [{WARN}] could not resolve key-based entries: {e}')
        transient.extend(stale)
        return []

    still_stale = []
    for entry in stale:
        try:
            rows = client.get_all_test_run_results(
                entry.get('project_key', DEFAULT_PROJECT_KEY),
                entry.get('run_key', ''))
        except Exception:
            # A read failure is transient. Keep the entry retryable rather
            # than parking it as permanently unreplayable.
            transient.append(entry)
            continue
        complete, translated = translate_keys_to_codes(entry, rows)
        if not translated:
            still_stale.append(entry)
            continue

        # SPLIT, rather than all or nothing. Measured against the live
        # project: the sink saves all 121 reviewed case keys when it can
        # resolve none, and not one of the ten runs resolves every key - the
        # best manages 82. All-or-nothing therefore parked every entry,
        # including the 82 results that would have landed, which made the
        # recovery the sink points the operator at dead end to end.
        #
        # Nothing is dropped: what resolved replays, and what did not stays
        # in the queue under its own entry, still holding its case keys.
        replay = dict(entry)
        replay['results'] = translated
        replay['result_count'] = len(translated)
        replayable.append(replay)

        if complete:
            print(f'  Resolved {len(translated)} case key(s) to codes for '
                  f'run {entry.get("run_key", "?")} - replaying.')
            continue

        done = {r['case'] for r in translated}
        by_code = {r.get('case_id'): r.get('code') for r in rows}
        leftover = [r for r in entry.get('results', [])
                    if str(r.get('case')) not in done
                    and by_code.get(str(r.get('case'))) not in done]
        print(f'  Resolved {len(translated)} of '
              f'{len(entry.get("results", []))} case key(s) for run '
              f'{entry.get("run_key", "?")} - replaying those. The other '
              f'{len(leftover)} name cases this run does not contain, so they '
              f'go to {UNREPLAYABLE_PATH.name}.')
        if leftover:
            rest = dict(entry)
            rest['results'] = leftover
            rest['result_count'] = len(leftover)
            still_stale.append(rest)
    return still_stale


def _park_unreplayable(stale: list[dict]) -> None:
    """Move entries that cannot be replayed out of the retry log.

    Retrying them would send case keys, which the API accepts and ignores
    while answering success - so the log would empty and nothing would have
    been written.
    """
    UNREPLAYABLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing = []
    if UNREPLAYABLE_PATH.exists():
        try:
            existing = json.loads(UNREPLAYABLE_PATH.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, ValueError):
            existing = []
    write_json_atomic(UNREPLAYABLE_PATH, existing + stale)
    empty = [e for e in stale if not (e.get('results') or [])]
    keyed = len(stale) - len(empty)
    # A key that did not resolve names a case the run does not contain, and a
    # run's case set does not change, so there is no later attempt to keep it
    # for. Parking preserves it for a human to look at.
    print(f'  [{WARN}] {len(stale)} entry(ies) cannot be replayed. Moved to '
          f'{UNREPLAYABLE_PATH.name}.')
    if keyed:
        print(f'         {keyed} still name their cases by KEY rather than '
              f'code, because')
        print('         the run does not contain them. The results API')
        print('         ignores a case key and reports success anyway, so a '
              '"successful"')
        print('         retry of those would have written nothing.')
    if empty:
        print(f'         {len(empty)} hold no results at all. An empty upload '
              f'is accepted,')
        print('         verifies against nothing, and would clear the entry '
              'having sent')
        print('         nothing.')
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Retry failed QA Touch uploads')
    parser.add_argument('--dry-run', action='store_true',
                        help='Show pending retries without executing')
    parser.add_argument('--clear', action='store_true',
                        help='Clear failure log without retrying')
    args = parser.parse_args(argv)

    failures, code = _load_failures()
    if failures is None:
        return code

    print('\n=== Upload Retry ===')
    print(f'  Pending: {len(failures)} failed upload(s)')
    print()

    if args.clear:
        FAILURE_LOG_PATH.unlink()
        print(f'  Cleared {len(failures)} entries from failure log.')
        return 0

    replayable = [e for e in failures if _replayable(e)]
    stale = [e for e in failures if not _replayable(e)]
    transient: list[dict] = []

    # Key-based entries (saved when the run was unreachable) become
    # replayable once the run answers: resolve keys -> codes from its rows.
    if stale and not args.dry_run:
        stale = _resolve_stale(stale, replayable, transient)

    if args.dry_run:
        for i, entry in enumerate(failures, 1):
            tag = '' if _replayable(entry) else '  <- NOT replayable (case keys, not codes)'
            print(f'  {i}. Run {entry.get("run_key", "?")} - '
                  f'{entry.get("result_count", "?")} results - '
                  f'{entry.get("timestamp", "?")}{tag}')
        print('\n  Run without --dry-run to execute retries.')
        return 0

    if stale:
        _park_unreplayable(stale)

    if not replayable:
        if transient:
            # Key-based entries a network blip left unresolved: keep them
            # queued for the next run rather than dropping them.
            write_json_atomic(FAILURE_LOG_PATH, transient)
            print(f'  {len(transient)} entr(y/ies) could not be resolved this '
                  f'time and stay queued for retry.')
        else:
            FAILURE_LOG_PATH.unlink()
            print('  Nothing replayable remains. Failure log cleared.')
        return 1 if (stale or transient) else 0

    try:
        # From the submodule, so a consumer shim aliased to it stays
        # patchable. See the note in upload.py.
        from soterix_qa_shared.qatouch.client import QATouchClient
        client = QATouchClient()
    except Exception as e:
        print(f'  [{FAIL}] Cannot initialize QA Touch client: {e}')
        return 2

    remaining = []
    succeeded = 0

    for i, entry in enumerate(replayable, 1):
        run_key = entry.get('run_key', '?')
        project_key = entry.get('project_key', DEFAULT_PROJECT_KEY)
        results = [{'case': r['case'], 'status': r['status']}
                   for r in entry.get('results', [])]
        comment = entry.get('comment', 'Retry upload')

        if not results:
            # Belt and braces with _replayable: an empty POST is accepted,
            # verifies against nothing, and would clear the entry.
            print(f'  {i}. Run {run_key} holds no results - keeping it rather '
                  f'than sending an empty upload.')
            remaining.append(entry)
            continue

        print(f'  {i}. Retrying run {run_key} ({len(results)} results)... ', end='')
        try:
            client.update_test_run_results_by_code(
                project_key=project_key,
                test_run_key=run_key,
                results=results,
                comments=f'{comment} (retry)',
            )
            report = client.verify_results_landed(project_key, run_key, results)
        except Exception as e:
            print(f'[{FAIL}] {e}')
            remaining.append(entry)
            continue

        problems = report['mismatched'] + [f'{c}: not in run' for c in report['missing']]
        if problems:
            print(f'[{FAIL}] uploaded but {len(problems)} did not land: {problems[:3]}')
            remaining.append(entry)
        else:
            print(f'[{PASS}] verified {len(report["landed"])} in run')
            succeeded += 1

    # Transient key-based entries a network blip left unresolved stay queued
    # too, so a flaky read never drops a recoverable upload.
    remaining.extend(transient)
    if remaining:
        write_json_atomic(FAILURE_LOG_PATH, remaining)
        print(f'\n  Retried: {succeeded} succeeded, {len(remaining)} still failing')
        return 1
    FAILURE_LOG_PATH.unlink()
    print(f'\n  All {succeeded} retries succeeded and verified. Failure log cleared.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
