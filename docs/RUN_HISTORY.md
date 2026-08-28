# Run history

One short line per run, tracked in git. This is the only thing a test run
leaves behind that belongs in a repository.

## What is and is not tracked

| | Where | Tracked | Why |
|---|---|---|---|
| Traces, videos, screenshots, HTML | `reports/` | no | 4.4 GB after one week, and regenerable by re-running |
| Per-node-id record of the last run | `logs/last_run_results.json` | no | overwritten every run, per machine |
| Pending failed uploads | `logs/upload_failures.json` | no | a work queue, not a record |
| **One line per armed run** | **`history/YYYY-MM.jsonl`** | **yes** | **~350 bytes, and it answers what the evidence cannot** |

The evidence answers "what went wrong in this run". The ledger answers the
questions that need more than one run: *when did this test start failing*,
*is the suite getting slower*, *what did it look like before the release*.
Those cannot be reconstructed from a directory that only ever holds the
latest run, and QA Touch will not answer them either if it is ever migrated
away from.

## Only armed runs

A run is ledgered when it uploaded to QA Touch — that is, `--qatouch-upload`
*and* a run key. Nothing else is.

The reason is signal, not size. In one week of development, 71 runs were
archived locally; almost all were `pytest -k something` while working on a
single test. Ledgering those would bury the runs somebody actually meant, and
would leave every developer with a dirty working tree after each run.

"Armed" already means "this was an official run", so nobody has to remember
a flag. `--no-history` opts out of a single armed run.

## The row

```json
{"at": "2026-08-28T19:37:45+00:00", "branch": "main", "commit": "0f912d0",
 "env": "qa", "run_key": "d6pw", "project_key": "JlW1", "target": ["tests/ui"],
 "dur_s": 1840.0, "exit": 1,
 "counts": {"passed": 598, "failed": 2, "skipped": 4},
 "failed": ["tests/ui/views/test_views_basic.py::test_grid"]}
```

- `dur_s` is **wall clock**, not the sum of test durations. Collection,
  fixtures and teardown are the difference, and on a Playwright suite they
  are often most of it.
- `failed` is sorted, so two runs diff cleanly when collection order shifts.
- A catastrophic run (the environment is down, everything fails) caps the
  list at 25 names and adds `failed_truncated`. The **count stays exact**;
  only the names are capped, so one bad night cannot write a 60 KB line.
- `branch` and `commit` are best effort. A tarball checkout or a machine with
  no git records `null` rather than failing the run.

## Rules for the file

**Append only.** A row that changes is a row that cannot be trusted. Never
edit or reorder existing lines.

**One file per month.** Keeps concurrent appends apart and keeps any one file
readable.

**On a merge conflict, keep both lines** in timestamp order. That is always
the right resolution — two runs happened, and both did.

Add to each consumer repo's `.gitignore`, so the directory is tracked while
everything else under it is not:

```
logs/
reports/
!history/
```

## Reading it

When did a test start failing:

```bash
grep -l . history/*.jsonl | xargs cat \
  | jq -r 'select(.failed[]? | test("test_grid")) | "\(.at) \(.commit)"'
```

Is the suite getting slower — duration of full-suite runs over time:

```bash
cat history/*.jsonl \
  | jq -r 'select(.counts.passed > 500) | "\(.at) \(.dur_s)s \(.counts.failed) failed"'
```

Which tests fail most often:

```bash
cat history/*.jsonl | jq -r '.failed[]?' | sort | uniq -c | sort -rn | head
```
