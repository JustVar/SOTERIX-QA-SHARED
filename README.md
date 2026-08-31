# soterix-qa-shared

The QA Touch pipeline the Soterix QA repos share: an API client, a pytest
result reporter, and the tools that replay a failed upload.

The UI repo and the API repo each carry their own copy of the client and the
reporter, and the copies had already diverged. Every new product repo would
multiply that. This package is the one copy they will share: at migration
steps 5 and 6 each old import path in a consumer becomes a thin shim
pointing here.

**Neither consumer has migrated yet.** The UI repo has the shims on a
rehearsal branch; the API repo does not reference this package at all. See
`STATUS.md` for where that stands.

## Install

```bash
pip install -e ../soterix-qa-shared            # the client only
pip install -e ../soterix-qa-shared[reporter]  # adds pytest, for a test repo
```

The base install is deliberately client-only, so a script that just talks to
QA Touch does not drag in a test runner.

## What it does

**Records a test run.** The pytest plugin collects one result per test and
hands the whole run to every registered destination:

| Destination | When | Where it writes |
|---|---|---|
| Local run record | every session that ran a test | `logs/last_run_results.json` |
| Run ledger | runs that upload | `history/YYYY-MM.jsonl` (meant to be tracked) |
| QA Touch upload | when armed | the QA Touch test run |

**Uploads results to QA Touch, and checks that they landed.** This API
answers success for uploads it silently discards, so every upload is read
back before it counts as done. A run that resolved no case codes is a
failure written to the retry log, never an empty success.

**Replays what failed.** `retry_uploads` resolves case keys to case codes and
sends them again, and an entry only leaves the log once the run shows the
status that was sent.

## Using it in a test repo

Arm the upload for a session:

```bash
pytest --qatouch-upload --qatouch-run <RUN_KEY>
# or: QATOUCH_UPLOAD=1 QATOUCH_RUN_KEY=<key> pytest
```

Replay anything that failed to upload:

```bash
python tools/retry_uploads.py --dry-run   # show what is pending
python tools/retry_uploads.py             # send it
```

Opt out of the local artifacts for one run with `--no-run-record` or
`--no-history`.

Credentials come from the environment - `QATOUCH_API_KEY` and
`QATOUCH_DOMAIN`. The package never reads a repo's `.env`; the consumer loads
it.

## Adding a new destination

Results are keyed by pytest node id, not by QA Touch case key, so a
destination that has never heard of QA Touch can still read them. Subclass
`ResultSink`, take the results and the run's facts, and write wherever you
like:

```python
from soterix_qa_shared.qatouch.results import ResultSink

class JUnitSink(ResultSink):
    name = 'junit'

    def finish(self, results, context):
        ...   # results: one RunResult per node id
              # context: duration, branch, commit, env, target
```

A sink must not raise - one broken destination must not cost a run its other
destinations, and must never fail the test session itself.

## Where to read next

| | |
|---|---|
| `docs/RULES.md` | What must stay true. Read before changing packaging, shims, or anything that writes a file. |
| `STATUS.md` | Where the migration stands and what comes next. |
| `docs/RUN_HISTORY.md` | What a run leaves behind, and why one path is tracked. |
| `AGENTS.md` | Guidance for AI coding tools working in this repo. |
| `docs/STYLE.md` | The writing rules, in short. Full version: `docs/writing/ste.md`. |

## Development

```bash
pip install -e .[dev]
python -m pytest tests/ -q      # 154 tests, all offline: no network, no browser
python -m ruff check .
python tools/prose_check.py     # the writing rules, mechanical ones only
```

The dev toolchain is pinned exactly rather than floored. A floating pin makes
a lint verdict depend on when a machine last installed, which is how a change
passes on one machine and fails on another with no code difference.
