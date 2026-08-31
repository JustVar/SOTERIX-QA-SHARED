# soterix-qa-shared

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

## Where it stands

**Steps 1, 2a and 3 complete.** 241 tests, ruff clean, wheel builds and imports in a
clean interpreter.

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

Two design decisions worth knowing before reading the code:

**Node id is the identity, not `case_key`.** A case key is not unique (42 are
claimed by more than one test), not always present (unmapped tests have
none), and is QA Touch's word for a test that a Jira filer or dashboard has
no use for. The reporter records one `RunResult` per node id; folding to
cases is the upload sink's private step.

**`ResultSink` is the extension point.** Three sinks today: the local run
record, the tracked run ledger, and the QA Touch upload. A JUnit writer or a
dashboard feed plugs in without reopening the reporter. Each sink decides
when it applies, which is why the record is written on
every session that ran a test and the ledger only on armed ones.

## Next, in order

| Step | State | What |
|---|---|---|
| 0 | mostly | Repo, CI, interpreter range, deploy key done. Shim matrix open |
| 1 | **done** | Client, reporter, sinks, retry CLI |
| 2a | **done** | Moved `products`, `paths`, `branding` |
| 2b | deferred | The rest of the reporting stack |
| 3 | **done** | Jira layer |
| 4 | **done** | Shims + guards, tagged **v0.16.0** |
| 5 | 5a done | API repo: pin landed; 5b shims next |
| 6 | 6a done | UI repo: pin landed; shims proven on a branch |
| 7 | | Docs |

**0. The guards later steps depend on.** The package's CI is in place
(`.github/workflows/ci.yml`): tests, ruff and the prose check across a
3.11 to 3.14 matrix; a wheel job that installs the BUILT wheel, imports
every module under a base install, checks `py.typed` shipped, and asserts
the reporter fails naming `pytest` (rule 2's `exc.name` branch depends on
that staying true); and the tag-equals-version guard of rule 4, which runs
only on a `v*` tag.

NOT built, and step 0 does not close without it: a per-interpreter
shim-identity matrix. `ci.yml` has three jobs and none of them builds a
shim. It belongs in the consumer repos and lands with steps 5 and 6.

*Governance files.* `README.md`, `AGENTS.md`, `docs/STYLE.md` and
`docs/writing/ste.md` are in place, and `tools/prose_check.py` enforces the
mechanical writing rules. `docs/CONVENTIONS.md` and a merge runbook are not
written. Still missing: a `MEMORY.md` pointer, and a run of
`~/.claude/memory_lint.py`.

*The semicolon rule is stated but not enforced anywhere.* Measured
2026-08-28: the UI README joins clauses with 14 of them, `docs/STYLE.md`
itself uses 4, and this repo has 21. The prose checker reports them under
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

**7. Docs.** Both README Quickstarts, the merge runbook's contributor steps,
and credential setup for every QA team member (or publish to an index they
already reach).

## Follow-ups the migration must not forget

- The reporter's lint findings moved with the code. The UI repo silenced none
  of them. It carried 10. Nine are now silenced by file in this package's
  `ruff.toml` with the reason written down, and the tenth was a real defect,
  fixed. When the UI's reporter becomes a shim those 10 leave its lint
  output; that drop is the move, not a weakened guard.
- The package docstring and `pyproject` description must widen in the same
  commit that lands a new layer. Rule 4 forces a version bump there anyway,
  so metadata and contents move together.
