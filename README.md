# soterix-qa-shared

The QA Touch reporting pipeline for the Soterix test repositories, as one
installable Python package. It stops the API client, the pytest reporter and
the Jira layer from drifting apart in each repository that uses them.

Two repositories use it today: Soterix-WebApp-QA and Soterix-API-QA. Both
import it through thin shims, so their old import paths still work.

## Install

```bash
pip install -e ../soterix-qa-shared              # client only
pip install -e "../soterix-qa-shared[reporter]"  # adds pytest, for a test repo
pip install -e "../soterix-qa-shared[jira]"      # adds the jira package
pip install -e "../soterix-qa-shared[dev]"       # pytest, ruff, pytest-html
```

A test repository does not install it this way. Its `requirements.txt`
already names the package, so `pip install -r requirements.txt` is enough.
Install it editable only to change it.

The base install stays client-only on purpose. A script that reads QA Touch
does not need a test runner.

## Report a test run to QA Touch

Run the suite with the upload armed and a run key:

```bash
pytest --qatouch-upload --qatouch-run d6pw
```

The run key is the short code in the QA Touch run's URL. Nothing uploads
without both the flag and the key, so an ordinary `pytest` never writes to
QA Touch.

The summary prints what happened:

```
  Mapped:     18 tests -> 5 cases
  Uploaded:   5 results
  Run coverage: 5 case(s) in PgGMv; this run set 5
  Passed:     5
```

## What it does

**Records every run.** The pytest plugin collects one result per test node
id, then hands the finished run to each destination in turn.

**Uploads to QA Touch, and checks the results landed.** The API answers
success for uploads it discards, so the plugin reads the run back and
compares each status. A run that resolved no case codes fails loudly.

**Reports what the run left unset.** The read-back also names cases in the
run that no selected test covered. A green upload over a half-filled run is
otherwise invisible.

**Replays failed uploads.** Failures go to a log with enough detail to send
them again later.

**Files a Jira bug, when told to.** Bug creation needs an environment flag
and the `jira` package. Without both, it describes the issue it would file.

## Options

Flags the pytest plugin adds:

| Flag | Effect |
|---|---|
| `--qatouch-upload` | Upload results after the session. Also `QATOUCH_UPLOAD=1`. |
| `--qatouch-run KEY` | The QA Touch run to write to. Also `QATOUCH_RUN_KEY`. |
| `--no-run-record` | Skip `logs/last_run_results.json` for this run. |
| `--no-history` | Skip the ledger row for this run. |

Environment variables:

| Variable | Used for |
|---|---|
| `QATOUCH_API_KEY` | QA Touch authentication. Required to upload. |
| `QATOUCH_DOMAIN` | The QA Touch subdomain. |
| `QATOUCH_PROJECT_ID` | Default project key. |
| `QATOUCH_RUN_KEY` | Run key, instead of `--qatouch-run`. |
| `QATOUCH_UPLOAD` | Arms the upload, instead of `--qatouch-upload`. |
| `ENV` | The environment name recorded with each run. |
| `JIRA_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` | Jira authentication. |
| `JIRA_PROJECT_KEY` | Default Jira project. |
| `BUG_CREATION_ENABLED` | Arms real bug creation. Any other value files nothing. |

## Files it reads and writes

Paths are relative to the consumer repository's root.

| Path | Tracked | What it holds |
|---|---|---|
| `config/qatouch_case_mapping.json` | yes | Node id to case key. The only place mappings live. |
| `history/YYYY-MM.jsonl` | yes | One line per armed run. See `docs/RUN_HISTORY.md`. |
| `logs/last_run_results.json` | no | Every result from the last run. |
| `logs/upload_failures.json` | no | Uploads to replay. |
| `logs/upload_failures_unreplayable.json` | no | Failures no replay can fix. |

## Command line tools

Replay uploads that failed:

```bash
python -m soterix_qa_shared.qatouch.retry --dry-run   # show what is pending
python -m soterix_qa_shared.qatouch.retry             # send it
python -m soterix_qa_shared.qatouch.retry --clear     # discard the log
```

A test repository reaches the same CLI as `python tools/retry_uploads.py`.

Fail a run that collects fewer tests than the last recorded count. Run this
from a test repository, where `tools/collect_floor.py` supplies that
repository's suite list:

```bash
python tools/collect_floor.py            # check
python tools/collect_floor.py --update   # record the current counts
```

The gate itself is `soterix_qa_shared.gates.collect_floor`. pytest exits 5
on zero collected, but reports nothing when a suite only shrinks.

## What is in the package

| Module | What it holds |
|---|---|
| `qatouch.client` | The QA Touch API client, status codes, case code helpers. |
| `qatouch.reporter` | The pytest plugin: hooks, node id records, sink fan-out. |
| `qatouch.results` | `RunResult`, `RunContext`, `ResultSink`, the two local sinks. |
| `qatouch.upload` | The QA Touch sink: case folding, retry, read-back. |
| `qatouch.retry` | The replay CLI. |
| `qatouch.mapping` | Node id and case key helpers. |
| `qatouch.layout` | Where a consumer repository keeps each file. |
| `qatouch.artifacts` | The frozen shapes of the JSON other tools parse. |
| `jira.client` | Creates an issue, or describes the one it would create. |
| `jira.fields` | Maps test data onto a project's custom fields. |
| `reporting.products` | QA Touch project keys and the product each one stands for. |
| `reporting.paths` | Fits a file name under the Windows 260 character limit. |
| `reporting.branding` | The company logo, embedded into a report page. |
| `gates.collect_floor` | Fails a run when a suite collects fewer tests. |

## Add a destination

`ResultSink` is the extension point. Three sinks ship today: the local run
record, the tracked ledger, and the QA Touch upload. A JUnit writer or a
dashboard feed plugs in without touching the reporter.

```python
from soterix_qa_shared.qatouch.results import ResultSink

class JUnitSink(ResultSink):
    name = 'junit'

    def finish(self, results, context):
        if not results:          # a dry pass ran no tests
            return
        write_junit(results, context.run_key)

    def summary(self):
        return ['  JUnit: written']
```

Each sink decides when it applies. The run record writes on every session
that ran a test. The ledger writes only on armed runs.

## Limits

- The reporter needs pytest 8 or 9. A consumer pins its own version.
- Python 3.11 or later. CI tests 3.11 through 3.14.
- Bug creation stays off until `BUG_CREATION_ENABLED` is set and the `jira`
  package is installed.
- One case key can cover several tests. The worst status wins.
- The package holds no repository-relative path. A consumer passes its root.

## Development

```bash
pip install -e ".[dev]"
python -m pytest                # all offline: no network, no browser
python -m ruff check .
python tools/prose_check.py     # the writing rules, mechanical ones only
```

Test a change against a real consumer before you tag it. The unit tests here
cannot see the shim seam. A change has passed them while breaking both
repositories.

## Where to read next

| | |
|---|---|
| `docs/CONVENTIONS.md` | How code gets written here, and the trap behind each rule. |
| `docs/RULES.md` | What must stay true. Read before changing packaging or shims. |
| `docs/MERGE_RUNBOOK.md` | The two remotes, and the five steps of a release. |
| `docs/RUN_HISTORY.md` | What a run leaves behind, and why one path is tracked. |
| `STATUS.md` | Where the work stands and what comes next. |
| `docs/STYLE.md` | The writing rules, in short. Full version: `docs/writing/ste.md`. |
| `AGENTS.md` | Guidance for AI coding tools working in this repository. |
