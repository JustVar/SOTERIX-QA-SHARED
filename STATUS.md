# Soterix QA Shared

One installable package for the QA Touch pipeline, so the UI repo, the API
repo and future product repos (Mobile, C2C, Neurosen) share one copy instead
of maintaining divergent ones. Each old import path becomes a thin
compatibility shim.

- **What must stay true:** [`docs/RULES.md`](docs/RULES.md). Nine rules and
  the compatibility contract. Read it before touching packaging, shims, or
  anything that writes a file.
- **What a run leaves behind:** [`docs/RUN_HISTORY.md`](docs/RUN_HISTORY.md).
  why `reports/` and `logs/` stay out of git and `history/` goes in.
- **Why a decision was made:** the QA program register (local, in the API
  repo at `docs/local/QA_PROGRAM_REGISTER.md`).
- **This file:** where the work stands and what comes next.

Renamed from `SOTERIX-QA-CORE` on 2026-08-28: "Core" read as **NexaiQ-Core**,
which is a product, not a library. GitHub redirects old URLs, so a stale
remote keeps working silently. Check `git remote -v`, do not trust a
successful push.

## Install

```bash
pip install -e ../soterix-qa-shared            # client only
pip install -e ../soterix-qa-shared[reporter]  # adds pytest, for test repos
```

Base install stays client-only so a script consumer (the UI canary job) does
not drag in a test runner.

## Where It Stands

**Steps 1 to 6 complete.** 317 tests, ruff clean, wheel builds and imports in a
clean interpreter. Both consumers are merged to main and green on Linux CI
against the v0.20.0 tag.

| Module | Lines | What it is |
|---|---|---|
| `qatouch.client` | 967 | The QA Touch API client |
| `qatouch.upload` | 522 | The upload sink: case folding, retry, read-back |
| `qatouch.reporter` | 430 | pytest hooks, node-id records, sink fan-out |
| `qatouch.retry` | 260 | The failed-upload replay CLI |
| `qatouch.results` | 348 | `RunResult`, `RunContext`, `ResultSink`, two local sinks |
| `qatouch.mapping` | 51 | Node-id/case-key helpers |
| `qatouch.artifacts` | 32 | Frozen shapes of the JSON other tools parse |
| `qatouch.layout` | 30 | Where a consumer repo keeps its files |
| `reporting.products` | 121 | QA Touch project keys and their product names |
| `reporting.paths` | 90 | Fitting a name under Windows' 260-char limit |
| `reporting.branding` | 75 | The company logo, embedded into a page |
| `jira.fields` | 243 | Mapping test data onto discovered custom fields |
| `jira.client` | 208 | Creating an issue, or describing the one it would create |
| `gates.collect_floor` | 140 | Failing a run when a suite collects fewer tests than before |
| `reporting.htmlblocks` | 97 | Inline evidence blocks for a pytest-html report |

Two design decisions worth knowing before reading the code:

**Node id is the identity, not `case_key`.** A case key is not unique (42 of
the 302 distinct keys are claimed by more than one test, covering 118 of the
378 mappings; none of the 121 reviewed mappings collide), not always present
(unmapped tests have none), and is QA Touch's word for a test that a Jira
filer or dashboard has no use for. The reporter records one `RunResult` per node id; folding to
cases is the upload sink's private step.

**`ResultSink` is the extension point.** Three sinks today: the local run
record, the tracked run ledger, and the QA Touch upload. A JUnit writer or a
dashboard feed plugs in without reopening the reporter. Each sink decides
when it applies, which is why the record is written on
every session that ran a test and the ledger only on armed ones.

## Next, in Order

| Step | State | What |
|---|---|---|
| 0 | **done** | Repo, CI, interpreter range, deploy key, shim matrix |
| 1 | **done** | Client, reporter, sinks, retry CLI |
| 2a | **done** | Moved `products`, `paths`, `branding` |
| 2b | **done** | The whole reporting stack extracted: media, run_archive, run_report, health, email, and the reporting layout |
| 3 | **done** | Jira layer |
| 4 | **done** | Shims + guards, tagged **v0.16.0** |
| 5 | **done** | API repo: pinned and shimmed, on `shim-migration` |
| 6 | **done** | UI repo: pinned and shimmed, on `shim-rehearsal` |
| 7 | **done**, one blocker | Docs. Access for two teammates is outstanding |

**0. The guards later steps depend on.** The package's CI is in place
(`.github/workflows/ci.yml`): tests, ruff and the prose check across a
3.11 to 3.14 matrix; a wheel job that installs the BUILT wheel, imports
every module under a base install, checks `py.typed` shipped, and asserts
the reporter fails naming `pytest` (rule 2's `exc.name` branch depends on
that staying true); and the tag-equals-version guard of rule 4, which runs
only on a `v*` tag.

Built 08/31 and step 0 closes. The per-interpreter shim-identity matrix
lives in each consumer's `collect-gate.yml`, because `ci.yml` here has no
shim to build. Each gate had pinned ONE interpreter, the UI on 3.14 and the
API on 3.12, so the guards had run on one version per repo and no version
had run both repos' shims. Measured on the first run: all 16 UI guards and
all 13 API guards pass on 3.11, 3.12, 3.13 and 3.14. Only the guards are
matrixed. What varies by interpreter is module identity, patch
reachability and CLI dispatch, which is what they assert.

*Governance files.* `README.md`, `AGENTS.md`, `docs/STYLE.md` and
`docs/writing/ste.md` are in place, and `tools/prose_check.py` enforces the
mechanical writing rules. `docs/CONVENTIONS.md` and a merge runbook are not
written. Still missing: a `MEMORY.md` pointer, and a run of
`~/.claude/memory_lint.py`.

*The semicolon rule is stated but not enforced anywhere.* Measured
2026-08-31: the UI README joins clauses with 14 of them, `docs/STYLE.md`
itself uses 4, and this repo has 22, spread over `STATUS.md` (8),
`docs/RULES.md` (8), and two each in `README.md`, `AGENTS.md` and
`docs/RUN_HISTORY.md`. The two documents written for step 7 have none. The prose checker reports them under
`--warnings` rather than failing, because failing every repo on day one
teaches people to skip the check. Sweeping all three repos is a decision
nobody has taken yet.

*The interpreter range is decided:* `>=3.11`, and the matrix exercises
3.11, 3.12, 3.13 and 3.14. The floor is measured rather than assumed: an
AST scan finds nothing newer than 3.11 in the package, and `datetime.UTC`
in `qatouch/reporter.py` is what sets it. Before this, only 3.14 had ever
run the package, while the API repo's gate ran 3.12.

*Pin the lint toolchain in every consumer.* Both carry `ruff>=0.15` and had
drifted to 0.16.1 (UI) and 0.16.2 (API). Benign when measured (identical
415-rule set, both clean), but a floating pin makes a lint verdict depend on
when a machine last installed. Steps 5a and 6a touch both requirements files
anyway.

*The credential is provisioned, and it is not a PAT.* A read-only DEPLOY KEY
on `SOTERIX-QA-SHARED`, its private half held as `QA_SHARED_DEPLOY_KEY` in
both consumers. A deploy key does not expire, which a PAT does - and this
org has already lost a secret that way. Proved on all three properties
before anything was wired to it: it reads the shared repo, a push is
refused, and it cannot see either consumer.

**Tagged: v0.16.0.** Cut from a pristine clone, not the working tree. The
wheel builds, all 16 modules import under a base install, `py.typed` ships,
and the reporter refuses to import without its extra while naming `pytest` -
which is what rule 2's shim guard branches on. The UI repo's 16 shim guards
and its 618 offline tests pass against that wheel.

**1. Remaining.** One live upload, to verify the two API-repo behaviours
that unification fixes: its run-key identifier and case_key fallback are the
accepted-but-ignored forms, and its playwright branch calls
`update_playwright_results`, which exists in no client (opt-in, and the
`AttributeError` is swallowed, so the symptom is three retries then a
zero-upload run). Needs a real QA Touch run, so it goes with step 5b.

**2a. Done.** `products`, `paths` and `branding` are in
`soterix_qa_shared.reporting`, verified against the originals across 585
inputs with zero mismatches. Two seams the move had to open:

- `PATH_RULES` ships **empty**. A rule about `tests/ui/c2c/` describes one
  repo's folders, and shipping it would route another repo's tests to a
  product whose run key the session does not have. The consumer calls
  `products.configure_path_rules()`.
- `branding` took its logo paths from `Path('assets')`, relative to the
  working directory. That worked only because every caller ran from the repo
  root. It now takes a root through `configure()`, and clears its logo cache
  there, so a rebuilt PNG at the same path is re-read.

Still open from 2a: the report-title hook. The API conftest has one and the
UI has none, so moving it as an **opt-in** hook means *giving* the UI a
title it lacks. That is a deliberate decision nobody has taken.

One test stayed in the UI repo: it reads `docs/QA_TOUCH_INTEGRATION.md` and
asserts the table matches. That doc is the source of truth for the table and
it lives in the consumer, so the test belongs there and will point at the
package through the shim.

**2b. Deferred, and needs its own design pass first.** `run_archive` (1124),
`run_report` (1746), `report_media` (400), `health_summary` (718),
`email_client` (113) stay in the UI repo until a second consumer needs them.
The design pass must cover the exported surface including private names
consumers already import (`_write_index`), the Playwright touchpoints, and an
evidence type carrying what the renderer consumes. `TestRecord` has separate
`screenshots`/`frames`/`traces`/`videos` plus `headline`, `signature`,
`digest`, `seen`, `history`.

**3. Done.** `JiraClient` and the field mapper are in
`soterix_qa_shared.jira`. Deciding *whether* a failure deserves a bug stays
in the UI repo: `multi_gate_validation`, the flaky database and the approval
loop did not move.

**Two latent bugs, not one.** Both had the same shape, and both were
reproduced against the original before being fixed. Work that belonged after
the create was indented inside `if story_link:`, so it happened only when a
caller passed a story to link to:

- the `return`, so a real create with no story link returned None. The issue
  existed in Jira; the review loop raised on `issue.get('key')`, caught it,
  printed "Item remains pending" and left the item queued. Approving it
  again files a duplicate.
- the test-bug marking, so a `[TEST]` summary filed without a story link got
  neither the `automation-test-DELETE-ME` label nor the admin comment. Test
  bugs would accumulate in a real project with nothing marking them
  disposable.

Neither had been seen, because `BUG_CREATION_ENABLED` is false in every
checked-in env file and the dry-run path returns before reaching them. They
would have appeared on the first real filing.

Two smaller repairs came with the move. The field loader read its JSON on
every lookup, so one `map_bug_fields` call did more than a dozen reads of
the same file, and the mapper held a constructor snapshot that could
disagree with what those reads returned. It now reads once, keyed by path.
And `MAPPING_JSON` was `__file__`-relative, which rule 5 forbids; it is
injected.

**4. Shims, guards, then tag.** Module-alias shims per rules 2-3 with rule
8's policy injection. The Jira shim is the awkward one: `JiraClient` and the
mapper move, but they live inside `tools/smart_jira_bug_creator.py` and
`config/jira_field_mapping.py` alongside code that stays, so those two files
become partial shims (import the moved names, keep the rest) rather than
whole-module aliases. That is a shape rule 2 does not cover, and it needs
its own guard: the names must resolve to the package, and the code that
stayed must still work. Copy `docs/shim_guard_template.py` into each consumer
as `tests/plugins/test_shims.py` and edit the two THIS REPO markers. The
template is proven: green against a real shimmed repo, and failing under a
star-import shim (4 tests), a plain alias on a CLI module (1), and a missing
policy injection (1), while the consumer's own suite does not move under any
of them. Then bump `[project] version` and tag.

**5. API repo.** 5a: add the pinned dependency to `requirements.txt`, update
`collect-gate.yml`'s header comment (it is no longer credential-free), land
green alone. 5b: land the shims, verify with one real QA Touch upload.

**6. UI repo.** 6a pin, 6b shims. The UI repo has **no merge-time gate at
all**. Its only workflow is `qa-nightly.yml` (canary paused since
2026-08-18; plus a workflow_dispatch smoke job). Add a collect gate as part
of this migration, or accept that shim breakage is caught only by local runs.
The canary needs the package, base install only.

*Announce, do not let people discover:* both repos will start writing
`logs/last_run_results.json` on ordinary local runs. That is the intended fix
(the flaky history and the run cross-check finally see unarmed runs), but it
changes what every developer's `pytest` leaves behind.

*Each consumer's `.gitignore` needs `!history/`*, or the ledger is written
and silently never committed, the worst of both outcomes. Add it in the
same commit as the pin, and say in the migration note that an armed run now
produces a file to commit.

**7. Docs.** Done 08/31, except one thing docs cannot fix.

Both README Quickstarts are corrected, and the setup they describe got
shorter rather than better explained. The API repo's never installed the
package at all: measured on a clean venv, following it exactly left
`pytest tests/plugins` unable to collect, stopping at `ImportError: ...
soterix-qa-shared is not installed`. The UI repo's had the install line but
described it as "the second line" when it is the fifth.

**Setup is now one command.** `requirements.txt` in each consumer names this
package over HTTPS, which is what the team's git already speaks, so the
credential used for `git clone` is the one pip uses. Measured on a clean
venv: `pip install -r requirements.txt` then `pytest tests/plugins` gives 13
passed, with no second clone and no editable install.

That deleted `requirements-shared.txt` from both repos, and with it the rule
that one requirements file was for CI and the other was not. CI keeps the
read-only deploy key and rewrites the single HTTPS url to SSH with `git
config url. ... .insteadOf`. Cloning this repo is now only for CHANGING the
package, where an editable install overrides the pin.

`docs/CONVENTIONS.md` and `docs/MERGE_RUNBOOK.md` are written. Conventions
covers the testing standard, with each rule attached to the incident that
earned it. The runbook covers the two remotes and the five release steps,
because a merged change here reaches nobody until it is tagged AND each
consumer is repinned.

**The blocker is access, not documentation.** Measured 08/31, this repo has
three collaborators: JustVar and SoterixSystems as admins, cschmoker-soterix
as read. SandeepMuralidhara has write on BOTH consumer repos and
Raghavendra-aski has write on the UI repo, and neither can read this one, so
the clone step in both READMEs returns 404 for them and their checkout
cannot run. No credential setup fixes that. Two read grants do.

## What Is Left

Both consumers are merged to main, pinned to `v0.20.0`, and green on Linux
CI, including a shim matrix across 3.11 to 3.14.

| | UI repo | API repo |
|---|---|---|
| Shim matrix | 16 guards x 4 interpreters | 13 guards x 4 interpreters |
| Shims | 6 | 2 |
| Guards | 16 | 13 |
| Collected | 1416 | 84 |
| Floor watches | whole repo, offline suites | whole repo, internal, integration |

**The pipeline is proven end to end, from BOTH consumers.** The API repo
landed two armed runs in QA Touch run **PgGMv** on 08/31 (TR3681 to TR3685,
all Passed on read-back). The UI repo landed its licenses proof in run
**9e8Q7** on 09/01: 20 results, 19 Passed and 1 Blocked, all verified by an
independent read-back. The dry run before it also proved the read-back
guard with live data: an interceptor faked success, and the pipeline read
the run, saw 20 Untested, and queued everything for retry instead of
reporting done.

The first run selected `tests/integration_api` and set four of the five
cases, because the fifth maps to a test in `tests/internal_api`. Nothing in
the output said so, which is why `verify_results_landed` now reports run
coverage. The same run also wrote absolute paths into the ledger, fixed in
v0.20.0.

**Still not built:** the five big reporting modules (4,338 lines). Measured
09/01/2026 when PR #2's `_support/reporting.py` was examined for the
reconciliation: it overlapped with NONE of them, so it became
`reporting.htmlblocks` (the API repo's file is now a shim) and the five
still have exactly one consumer. The recorded gate stands: they move when
a second consumer needs them, after the design pass this file already
sketches (exported surface incl. `_write_index`, the Playwright
touchpoints, an evidence type for what the renderer consumes).

## Follow-ups the Migration Must Not Forget

- The reporter's lint findings moved with the code. The UI repo silenced none
  of them. It carried 10. Nine are now silenced by file in this package's
  `ruff.toml` with the reason written down, and the tenth was a real defect,
  fixed. When the UI's reporter becomes a shim those 10 leave its lint
  output; that drop is the move, not a weakened guard.
- The package docstring and `pyproject` description must widen in the same
  commit that lands a new layer. Rule 4 forces a version bump there anyway,
  so metadata and contents move together.
