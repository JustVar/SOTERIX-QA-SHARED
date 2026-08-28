# soterix-qa-shared

One installable package for the QA Touch pipeline, so the UI repo, the API
repo and future product repos (Mobile, C2C, Neurosen) share one copy instead
of maintaining divergent ones. Each old import path becomes a thin
compatibility shim.

- **What must stay true:** [`docs/RULES.md`](docs/RULES.md) — nine rules and
  the compatibility contract. Read it before touching packaging, shims, or
  anything that writes a file.
- **Why a decision was made:** the QA program register (local, in the API
  repo at `docs/local/QA_PROGRAM_REGISTER.md`).
- **This file:** where the work stands and what comes next.

Renamed from `SOTERIX-QA-CORE` on 2026-08-28: "Core" read as **NexaiQ-Core**,
which is a product, not a library. GitHub redirects old URLs, so a stale
remote keeps working silently — check `git remote -v`, do not trust a
successful push.

## Install

```bash
pip install -e ../soterix-qa-shared            # client only
pip install -e ../soterix-qa-shared[reporter]  # adds pytest, for test repos
```

Base install stays client-only so a script consumer (the UI canary job) does
not drag in a test runner.

## Where it stands

**Step 1 complete.** 130 tests, ruff clean, wheel builds and imports in a
clean interpreter.

| Module | Lines | What it is |
|---|---|---|
| `qatouch.client` | 967 | The QA Touch API client |
| `qatouch.upload` | 522 | The upload sink: case folding, retry, read-back |
| `qatouch.reporter` | 430 | pytest hooks, node-id records, sink fan-out |
| `qatouch.retry` | 260 | The failed-upload replay CLI |
| `qatouch.results` | 221 | `RunResult`, `ResultSink`, the local run record |
| `qatouch.mapping` | 51 | Node-id/case-key helpers |
| `qatouch.artifacts` | 32 | Frozen shapes of the JSON other tools parse |
| `qatouch.layout` | 30 | Where a consumer repo keeps its files |

Two design decisions worth knowing before reading the code:

**Node id is the identity, not `case_key`.** A case key is not unique (42 are
claimed by more than one test), not always present (unmapped tests have
none), and is QA Touch's word for a test that a Jira filer or dashboard has
no use for. The reporter records one `RunResult` per node id; folding to
cases is the upload sink's private step.

**`ResultSink` is the extension point.** A tracked run history, a JUnit
writer or a dashboard feed plugs in without reopening the reporter. The
local run record and the QA Touch upload are already two sinks — which is why
the record is written on every session, armed or not.

## Next, in order

| Step | State | What |
|---|---|---|
| 0 | partial | Repo, CI, interpreter range, PAT |
| 1 | **done** | Client, reporter, sinks, retry CLI |
| 2a | next | Move `products`, `paths`, `branding` |
| 2b | deferred | The rest of the reporting stack |
| 3 | | Jira layer |
| 4 | | Shims + guards, then tag |
| 5 | | Migrate the API repo (5a pin, 5b shims) |
| 6 | | Migrate the UI repo (6a pin, 6b shims) |
| 7 | | Docs |

**0 — the guards later steps depend on.** Add the package's CI: collect gate,
ruff lint (the ruff.toml is inert without one), the tag-equals-version guard
(rule 4), the wheel-install import job (rule 9), and a per-interpreter
shim-identity matrix.

*Decide the interpreter range here*, since it is that matrix's input. Both
repos' venvs run 3.14.6, the UI's workflows pin 3.14, the API's gate pins
3.12, and `pyproject` claims `>=3.11`. The real floor is 3.11
(`datetime.UTC`) but **only 3.14 is exercised** — so `>=3.11` is an
unverified claim. Run the matrix on 3.11/3.12/3.14, or narrow the range. The
matrix is preferable: the API gate already runs an interpreter nothing tests
the package on.

*Pin the lint toolchain in every consumer.* Both carry `ruff>=0.15` and had
drifted to 0.16.1 (UI) and 0.16.2 (API). Benign when measured — identical
415-rule set, both clean — but a floating pin makes a lint verdict depend on
when a machine last installed. Steps 5a and 6a touch both requirements files
anyway.

*Provision the read-only PAT under the same secret name in both orgs now.*
Step 5a lands a private pin that the API merge gate installs on every PR;
without the credential that PR turns the repo's only gate red.

Do **not** tag yet.

**1 — remaining.** One live upload, to verify the two API-repo behaviours
that unification fixes: its run-key identifier and case_key fallback are the
accepted-but-ignored forms, and its playwright branch calls
`update_playwright_results`, which exists in no client (opt-in, and the
`AttributeError` is swallowed, so the symptom is three retries then a
zero-upload run). Needs a real QA Touch run, so it goes with step 5b.

**2a.** Move the stdlib-only leaves `products`, `paths`, `branding` with their
266 test lines. The package owns the `PRODUCTS` table and `product_name`;
`PATH_RULES` stays consumer-supplied, since the c2c rule is meaningless in
the API repo. The report-title hook moves from the API conftest as an
**opt-in** hook — the UI has none today, so "own it once" means *giving* the
UI a title it lacks. Decide that deliberately.

**2b — deferred, and needs its own design pass first.** `run_archive` (1124),
`run_report` (1746), `report_media` (400), `health_summary` (718),
`email_client` (113) stay in the UI repo until a second consumer needs them.
The design pass must cover the exported surface including private names
consumers already import (`_write_index`), the Playwright touchpoints, and an
evidence type carrying what the renderer consumes — `TestRecord` has separate
`screenshots`/`frames`/`traces`/`videos` plus `headline`, `signature`,
`digest`, `seen`, `history`.

**3 — Jira.** Extract `JiraClient` and fix its latent bug: the
`return {'key': ...}` sits inside `if story_link:`, so a real create with no
story link returns None. Package `QATouchToJiraMapper` and the discovery CLI
with an injected mapping path. `multi_gate_validation`, the flaky DB and the
CLI review loop stay in the UI repo.

**4 — shims, guards, then tag.** Module-alias shims per rules 2-3 with rule
8's policy injection. Copy `docs/shim_guard_template.py` into each consumer
as `tests/plugins/test_shims.py` and edit the two THIS REPO markers. The
template is proven: green against a real shimmed repo, and failing under a
star-import shim (4 tests), a plain alias on a CLI module (1), and a missing
policy injection (1) — while the consumer's own suite does not move under any
of them. Then bump `[project] version` and tag.

**5 — API repo.** 5a: add the pinned dependency to `requirements.txt`, update
`collect-gate.yml`'s header comment (it is no longer credential-free), land
green alone. 5b: land the shims, verify with one real QA Touch upload.

**6 — UI repo.** 6a pin, 6b shims. The UI repo has **no merge-time gate at
all** — its only workflow is `qa-nightly.yml` (canary paused since
2026-08-18; plus a workflow_dispatch smoke job). Add a collect gate as part
of this migration, or accept that shim breakage is caught only by local runs.
The canary needs the package, base install only.

*Announce, do not let people discover:* both repos will start writing
`logs/last_run_results.json` on ordinary local runs. That is the intended fix
— the flaky history and the run cross-check finally see unarmed runs — but it
changes what every developer's `pytest` leaves behind.

**7 — docs.** Both README Quickstarts, the merge runbook's contributor steps,
and credential setup for every QA team member (or publish to an index they
already reach).

## Follow-ups the migration must not forget

- The reporter's lint findings moved with the code. The UI repo silenced none
  of them — it carried 10. Nine are now silenced by file in this package's
  `ruff.toml` with the reason written down, and the tenth was a real defect,
  fixed. When the UI's reporter becomes a shim those 10 leave its lint
  output; that drop is the move, not a weakened guard.
- The package docstring and `pyproject` description must widen in the same
  commit that lands a new layer. Rule 4 forces a version bump there anyway,
  so metadata and contents move together.
