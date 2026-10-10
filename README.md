# Soterix QA Shared

## About this repo

The test pipeline that two test repos share: Soterix-WebApp-QA (browser tests)
and Soterix-API-QA (API tests). Built by Justin Varughese, System Platform
QA/QC Manager, for Soterix Systems, an AI video-surveillance SaaS company.

- An upload to QA Touch (a test management tool that holds the team's test
  cases and the result of each test run) that reads every result back,
  because the QA Touch
  API answers success for results it discards.
- A run report page that names the shared cause of a group of failures and
  marks known defects apart from new ones.
- 850 tests of its own. 31 tagged releases, each one installed into both test
  repos as a package.
- Where to start:
  - The read-back upload
    ([src/soterix_qa_shared/qatouch/upload.py](src/soterix_qa_shared/qatouch/upload.py)).
  - The result sinks ([src/soterix_qa_shared/qatouch/results.py](src/soterix_qa_shared/qatouch/results.py)),
    the extension point that sends one run to several destinations.
  - The run page ([src/soterix_qa_shared/reporting/run_page.py](src/soterix_qa_shared/reporting/run_page.py)).
- Built with AI coding agents (Claude Code, OpenAI Codex, Microsoft 365
  Copilot) under the rules in [AGENTS.md](AGENTS.md).
- To see it run: `pip install -e ".[dev]"`, then `python -m pytest`. See
  "Development" below.
- The case study that covers all three repos:
  [qa-automation-case-study](https://github.com/JustVar/qa-automation-case-study).
- The two test repos:
  [Soterix-WebApp-QA](https://github.com/JustVar/Soterix-WebApp-QA) and
  [Soterix-API-QA](https://github.com/JustVar/Soterix-API-QA).
- Running it today: Soterix has since closed. This package needs no server,
  so its tests still run on any machine. CI ran on the company's copies of
  these repos and is switched off here.

## What this package does

This package holds the code that the Soterix test repositories share. It
does 3 jobs:

- It keeps the evidence of each test run and builds the pages that people
  read: the run report, the archive index and the health page.
- It sends results to QA Touch and makes sure that they arrived.
- It holds the gates that stop a bad change, such as a suite that collects
  fewer tests than before.

How it works: the package is a set of pytest plugins and command line
tools. A test repository keeps a built wheel of the package in its
`vendor/` folder and names it in `requirements.txt`. Thin shim modules in
that repository point at the package, so the old import paths still work.
Thus each repository runs the same reporter, the same client and the same
gates.

Two repositories use it: Soterix-WebApp-QA and Soterix-API-QA.

## Install

A test repository installs the package from its own `vendor/` folder. Its
`requirements.txt` holds two lines:

```
--find-links vendor
soterix-qa-shared==0.38.0
```

pip resolves `vendor` relative to the requirements file, so
`pip install -r requirements.txt` works from any folder, needs no access to
this repository and needs no credential. A new engineer needs read access
to the test repository and nothing else.

To change the package, clone this repository beside a test repository and
install it editable into that repository's venv. An editable install
overrides the vendored wheel until the next `pip install -r requirements.txt`.

```bash
git clone https://github.com/JustVar/SOTERIX-QA-SHARED.git ../soterix-qa-shared
pip install -e ../soterix-qa-shared              # client only
pip install -e "../soterix-qa-shared[reporter]"  # adds pytest, for a test repo
pip install -e "../soterix-qa-shared[jira]"      # adds the jira package
pip install -e "../soterix-qa-shared[dev]"       # pytest, ruff, pytest-html
```

The base install stays client-only on purpose. A script that reads QA Touch
does not need a test runner.

### Release a New Version

A change here reaches a test repository only as a new wheel in its
`vendor/` folder. The steps, in order:

1. Bump `version` in `pyproject.toml`. A wheel whose version did not change
   installs as a silent no-op that leaves the old code in place.
2. Run the tests, `ruff` and the prose check. All three must pass.
3. Commit and push. Wait for CI on `main` to pass.
4. Tag the commit and push the tag:
   `git tag -a vX.Y.Z -m "what changed"` then `git push origin vX.Y.Z`.
   CI checks that the tag equals the version.
5. Build the wheel: `python -m pip wheel . --no-deps -w dist`. The file is
   `dist/soterix_qa_shared-X.Y.Z-py3-none-any.whl`.
6. In each test repository: copy the wheel into `vendor/`, delete the old
   wheel, change the `==` version in `requirements.txt`, run
   `pip install -r requirements.txt`, run the offline suites, commit.

Each test repository has a test that fails when the installed version, the
`==` line and the wheel in `vendor/` disagree.

## Report a Test Run to QA Touch

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

## What It Does

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

| Variable | Used For |
|---|---|
| `QATOUCH_API_KEY` | QA Touch authentication. Required to upload. |
| `QATOUCH_DOMAIN` | The QA Touch subdomain. |
| `QATOUCH_PROJECT_ID` | Default project key. |
| `QATOUCH_RUN_KEY` | Run key, instead of `--qatouch-run`. |
| `QATOUCH_UPLOAD` | Arms the upload, instead of `--qatouch-upload`. |
| `ENV` | The environment name recorded with each run. |
| `QA_TRACES_PUBLISHED` | `0` when a run folder will be copied without its trace zips. The run page then says where the trace is instead of linking it. |
| `JIRA_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` | Jira authentication. |
| `JIRA_PROJECT_KEY` | Default Jira project. |
| `BUG_CREATION_ENABLED` | Arms real bug creation. Any other value files nothing. |

The package reads no tenant or machine variable. A run is described by
its environment name, the server host, the machine, the operator and the
account, all recorded in `summary.json`.

## Optional Hooks

A test repository can give the reporting pages facts the package cannot
know. Each hook is a function the repository registers at start-up. With
no hook, the page renders without that fact.

| Hook | What the repository supplies |
|---|---|
| `reporting.run_page.configure_axis(provider)` | Names for the columns of the run map. The provider gets the run's parameters and returns `{'noun': 'camera', 'columns': {param: {'group', 'label', 'title'}}}`, or `None`. Without it the columns show the parameter text. |
| `reporting.run_archive.configure_known_defects(provider)` | Which failures are app defects somebody already knows. The provider gets a node id and, on a failure, its first error line, and returns `{'since': date, 'what': one line}` or `None`. The run page then labels the row `Known defect` and counts it apart from the failures still to read. An empty list behind the provider labels nothing. |
| `reporting.products.configure_path_rules(rules)` | Which product a test path belongs to. |

## Files It Reads and Writes

Paths are relative to the consumer repository's root.

| Path | Tracked | What It Holds |
|---|---|---|
| `config/qatouch_case_mapping.json` | yes | Node id to case key. The only place mappings live. |
| `history/YYYY-MM.jsonl` | yes | One line per armed run. See `docs/RUN_HISTORY.md`. |
| `logs/last_run_results.json` | no | Every result from the last run. |
| `logs/upload_failures.json` | no | Uploads to replay. |
| `logs/upload_failures_unreplayable.json` | no | Failures no replay can fix. |

## Command Line Tools

Replay uploads that failed:

```bash
python -m soterix_qa_shared.qatouch.retry --dry-run   # show what is pending
python -m soterix_qa_shared.qatouch.retry             # send it
python -m soterix_qa_shared.qatouch.retry --clear     # discard the log
```

Soterix-WebApp-QA reaches the same CLI as `python tools/retry_uploads.py`.

Fail a run that collects fewer tests than the last recorded count. Run this
from a test repository, where `tools/collect_floor.py` supplies that
repository's suite list:

```bash
python tools/collect_floor.py            # check
python tools/collect_floor.py --update   # record the current counts
```

The gate itself is `soterix_qa_shared.gates.collect_floor`. pytest exits 5
on zero collected, but reports nothing when a suite only shrinks.

## What Is in the Package

| Module | What It Holds |
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
| `reporting.run_archive` | The pytest plugin that copies the evidence of each run into its own folder and builds the archive index. |
| `reporting.run_report` | Collects the facts of one archived run for its `run.html`: outcomes, errors, traces, the history of each test. |
| `reporting.run_page` | Renders `run.html` in 3 levels: the verdict, the causes and a map of the run, then 1 test in a side pane. A consumer names the map's columns with `configure_axis`. |
| `reporting.health` | Builds the health page from the archived runs. |
| `reporting.media` | The pytest plugin that saves one labeled screenshot for each UI test. |
| `reporting.live` | The record of a run in progress, and the estimate of the time that is left. |
| `reporting.layout` | Where a consumer repository keeps the evidence. |
| `reporting.email` | Sends a run report by email through Microsoft Graph. |
| `reporting.htmlblocks` | Evidence blocks for a pytest-html report. |
| `reporting.products` | QA Touch project keys and the product each one stands for. |
| `reporting.paths` | Fits a file name under the Windows 260 character limit. |
| `reporting.branding` | The company logo, embedded into a report page. |
| `gates.collect_floor` | Fails a run when a suite collects fewer tests. |

## Add a Destination

`ResultSink` is the extension point. Three sinks ship today: the local run
record, the tracked ledger, and the QA Touch upload. A new destination, such
as a JUnit writer or a dashboard feed, is a subclass of `ResultSink` and one
line in `_build_sinks` in `qatouch/reporter.py`, the function that picks the
sinks for a session. The reporter's hooks and the other sinks stay as they
are.

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

Then register it in `_build_sinks`:

```python
sinks.append(JUnitSink())
```

Each sink decides when it applies. The run record writes on every session
that ran a test. The ledger writes only on armed runs.

## Limits

- The reporter needs pytest 8 or 9. A consumer pins its own version.
- Python 3.11 or later is declared. CI ran 3.14, the version both test
  repositories use.
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

## Where to Read Next

| | |
|---|---|
| `docs/CONVENTIONS.md` | How code gets written here, and the trap behind each rule. |
| `docs/RULES.md` | What must stay true. Read before changing packaging or shims. |
| `docs/MERGE_RUNBOOK.md` | The remote, the change loop, and the six steps of a release. |
| `docs/RUN_HISTORY.md` | What a run leaves behind, and why one path is tracked. |
| `STATUS.md` | What the package holds today, and how the two test repositories install it. |
| `docs/STYLE.md` | The writing rules, in short. Full version: `docs/writing/ste.md`. |
| `AGENTS.md` | Guidance for AI coding tools working in this repository. |
