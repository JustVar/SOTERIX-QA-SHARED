# soterix-qa-core — build status

Shared QA pipeline for the Soterix QA repos (Foundations plan step 1). Ends
the divergence of the duplicated QA Touch tooling: one installable package
that the API repo, the UI repo, and future product repos (Mobile, C2C,
Neurosen) depend on, with a thin compatibility shim at each old import path.

Every mechanism below was proven by prototype across two adversarial audits
(2026-08-27, 22 agents). Claims that failed an audit are recorded as rules,
not as prose to be rediscovered later. Where a later step is genuinely
under-specified, this document says so rather than implying it is planned.

## Done

- Package scaffold: src layout, `pyproject.toml` (setuptools; base install is
  client-only, with `reporter`, `jira` and `dev` extras). Wheel build verified.
- **`soterix_qa_core.qatouch.client`** — the unified QA Touch client. Base is
  the UI implementation (the superset and the corrected one: its
  `create_project`/`create_module` use the endpoints that exist, and it
  carries `update_test_run_results_by_code`, `verify_results_landed`,
  `update_test_case`, the legacy-field aliases and all eight status codes).
  The API repo's `create_test_case_with_steps` is folded in. Ruff clean.
- Offline unit tests for the client helpers (`tests/test_client_helpers.py`).

## Rules (each proven by prototype; violate one and the migration breaks quietly)

1. **One plugin registration name, ever.** Listing both the shim path and the
   package path in `pytest_plugins` raises `ValueError: Plugin already
   registered under a different name` before any hook runs. The package must
   NOT ship a `[pytest11]` entry point while consumers keep the shim in
   `pytest_plugins`, or every run in both repos dies at configure time.
   Entry point OR shim, never both.

2. **Two shim shapes. Use the guarded one only where a CLI exists.**
   A plain alias breaks `python -m` (silent no-op, exit 0, nothing runs), so
   modules with a real CLI need the guard *before* the alias swap. Modules
   without a `main()` must NOT use the guarded form: it raises
   `AttributeError` on `python -m`.
   - **Guarded form** — today exactly one module qualifies: `run_report`
     (`def main(argv=None)`), plus any `tools/*` module that actually moves
     and has a `__main__` guard.
   - **Plain form** — everything else (`products`, `paths`, `branding`,
     `qatouch_client`, `qatouch_reporter`): same template minus the two guard
     lines.

   Canonical guarded template (verified: package absent gives the friendly
   message; package present with a missing dependency tells the truth;
   healthy import is 2 lines; `python -m` runs `main()`):

   ```python
   import sys
   try:
       from soterix_qa_core.qatouch import reporter as _mod
   except ModuleNotFoundError as exc:
       if (exc.name or "").split(".")[0] != "soterix_qa_core":
           raise                    # a dependency of the package is missing
       raise ModuleNotFoundError(
           "soterix-qa-core is not installed. "
           "Run: pip install -e ../soterix-qa-core"
       ) from None
   if __name__ == "__main__":       # guarded form only
       sys.exit(_mod.main())
   sys.modules[__name__] = _mod
   ```

   The `exc.name` check is load-bearing: without it, a missing dependency
   *of the package* is misreported as "the package is not installed", and
   step 2 deliberately puts Playwright rendering behind its own module.

3. **Registration is unconditional; the failure is legible.** Conditional
   registration was tested and rejected: with the package absent,
   `pytest --qatouch-upload` exits 4 with `unrecognized arguments` and never
   names the package, and `QATOUCH_UPLOAD=1` silently does nothing — a silent
   wrong result, which the house rules forbid. So the root conftest keeps
   `pytest_plugins` unconditional and relies on Rule 2's message (pytest
   promotes it to the last line on screen). Consequence to accept and write
   down: the package becomes a hard install dependency for *any* pytest run
   in either repo, so **drop the "reporting is opt-in" framing from both
   READMEs** and add the editable install to both Quickstarts and to the
   merge runbook's contributor steps.

4. **Every release tag MUST also bump `[project] version`.** Reproduced: with
   the version unchanged, `pip install -r requirements.txt` against a new tag
   pin is a silent exit-0 no-op that leaves the old code installed; `-U` does
   not rescue it (`--force-reinstall` does). Guards, both scheduled in step 0:
   a core-repo CI job that fails when a pushed tag does not equal
   `[project] version`, and a consumer-side assertion after install that
   `direct_url.json`'s `requested_revision` matches the pin.

5. **The package holds no repo-relative path constant.** Today's constants
   are CWD- or `__file__`-relative and live inside the consumer repo; after
   extraction `__file__` points into site-packages. Rootpath alone is not the
   answer either, because two documented CLIs (`python -m plugins.run_report`,
   `python run.py health`) run with no pytest config. So: consumers pass a
   root explicitly via a `configure(root: Path)` the shim calls — the pytest
   plugin supplies `config.rootpath`, the CLIs supply the repo root they
   resolve themselves. Exported names become **functions, not constants**, so
   `run.py` and `health_summary.py` stop importing frozen values.

6. **Read module globals at call time.** The path values stay monkeypatchable
   only if functions read the module global when called; a recompute-on-use
   variant silently ignored both test patches and wrote into the real repo.

7. **Guard every write inside a pytest hook.** An exception in
   `pytest_configure` aborts the whole session with `INTERNALERROR` (seen for
   real: a Windows 8.3-vs-long path made rootdir fall back to `C:\Users` and
   the write raised `PermissionError`). Wrap writes in try/except and degrade
   to no-reporting. Never compare a rootpath to `Path.cwd()`/`resolve()`
   without normalizing both.

8. **The mapping policy is injected by the shim, and it applies to the
   uploader only.**
   - Channel: the shim sets `_mod.MAPPING_POLICY = <callable>` *before*
     `sys.modules[__name__] = _mod`. (Not a constructor argument: the reporter
     is built inside `pytest_configure`, so a missing required argument is an
     `INTERNALERROR`, exactly what Rule 7 forbids. "Fail at import" is not
     achievable for a constructor and is not claimed here.)
   - Contract: `policy(path: Path) -> tuple[dict[str, str], dict[str, int]]`
     — the mapping and the counts, so the UI's "N of M rows are unreviewed"
     warning survives. Missing policy: `pytest_configure` degrades to
     no-upload with a loud terminal line.
   - UI injects `reviewed_lookup`; the API injects an explicit permissive
     loader (and see the contract below: whether the API should instead adopt
     the reviewed gate is an open decision, not a permanent exemption).
   - **Scope:** this governs the reporter's upload path only. The archive's
     display-only `load_case_mapping` keeps its permissive default and its
     optional-by-contract behaviour. The package will hold two mapping
     loaders with deliberately opposite policies; that is intended.

9. **Declare `package-data`** for any non-.py file the package needs, and add
   a CI job that installs the BUILT WHEEL and imports every subpackage — the
   editable install's `.pth` masks missing data files. (It does not mask a
   missing `__init__.py`: PEP 420 makes those importable either way.)

## The compatibility contract

- **The shared mutable artifact is the QA Touch project, not the JSON files.**
  `logs/upload_failures.json` and `logs/last_run_results.json` are per-repo,
  gitignored, and read only by the same repo's tools — *because* their writer
  paths resolve inside the consumer repo. After extraction that containment
  depends on Rules 5-7 being implemented correctly; it is not free.
- **Results are written per test RUN, not per case.** The only status-writing
  endpoint takes a run key, and the reporter refuses to register without one.
  Two suites reporting the same `case_key` into two different runs are
  therefore isolated — both results stand.
- **One `case_key` maps to one test function, across the union of all
  mappings.** The real hazard is *within-repo* duplication, where two tests
  share a key inside one run and the second genuinely overwrites the first.
  Measured baseline: UI 42 keys claimed by more than one node id (76
  redundant claims), API 5 (19). Collapsing pytest parametrization to
  distinct test functions: UI 38/54, API 4/6.
- **Cross-repo:** exactly one shared key exists across 409 rows — `WEnvKQ`
  (API `test_dashboard_apis`/`test_platform_apis::test_get_site_status`; UI
  `test_admin_sites.py::test_check_site_status`). It is **dormant**: the UI
  row is `reviewed: false` and the UI gates on reviewed, so it never uploads.
  Live only if someone reviews that row. This is a hygiene item, **not** a
  prerequisite for the migration.
- **The checker:** a console script in this package taking N mapping paths.
  Each repo's CI runs it over its own mapping (the case that actually
  matters); the cross-repo pass needs a location holding both checkouts, so
  name that owner before promising it. Fail only on a NEW duplicate versus a
  committed baseline, so existing debt does not block every PR. Reuse the
  UI's existing `tools/qatouch_mapping_audit.py` `shared` logic rather than
  writing a second one. Treat a missing `reviewed` key as "not gated", never
  `KeyError` (zero of the API's 31 rows carry it).
- **Product model, as it actually is today:** every mapped case is JlW1.
  `tests/ui/c2c/` is tracked (7 test files) and routed to 60W5 by
  `products.PATH_RULES`, but has **no mapping rows at all**, so it cannot
  report. The uploader and the reporting layer resolve product by different
  mechanisms — the reporter never imports `products.py`. Unifying `PRODUCTS`
  does not by itself unify resolution, and c2c uploads would additionally
  need a per-project run key.
- Record a minimum-core-version per consumer and note which core version
  changed upload semantics.

## Next, in order

**0. Stand the repo up, and land the guards that later steps depend on.**
`gh repo create` in Soterix-Systems; `origin` = JustVar carrying BOTH push
URLs; `company` = Soterix-Systems fetch-only (the verified workspace shape).
Copy the governance files that do not travel: `AGENTS.md`, `README.md`,
`ruff.toml`, `docs/writing/ste.md`, `docs/STYLE.md`, a merge runbook. Add a
`MEMORY.md` pointer and run `~/.claude/memory_lint.py`. Add the package's own
CI: collect gate, the tag-equals-version guard (Rule 4), the wheel-install
import job (Rule 9), and a per-interpreter shim-identity matrix. **Provision
the read-only PAT under the same secret name in both orgs now** — step 5a
lands a private pin into a requirements.txt that the API merge gate installs
on every PR, so without the credential that PR turns the repo's only gate
red. Do **not** tag yet.

1. **Reporter** — `soterix_qa_core.qatouch.reporter`, base = UI
   `plugins/qatouch_reporter.py` (a strict superset of the API copy).
   Retarget the lazy client import; apply Rules 5-8; bring `retry_uploads`
   along and freeze the `upload_failures.json` entry shape it parses.
   Two API-repo behaviours that unification fixes, to verify with one live
   upload: its run-key identifier and case_key fallback are the measured
   accepted-but-ignored forms, and its playwright branch calls
   `update_playwright_results`, which exists in no client. That branch is
   opt-in via `QATOUCH_USE_PLAYWRIGHT_ENDPOINT` and the AttributeError is
   swallowed, so today's symptom is three retries then a zero-upload run.

2. **Reporting stack — split, because the whole is a multi-week job.**
   Measured surface: 4,338 production lines and 3,354 test lines across
   `run_archive` (1124), `run_report` (1746), `report_media` (400),
   `health_summary` (718), `products` (105), `paths` (88), `branding` (44),
   `email_client` (113).
   - **2a (now):** move the genuinely shared, stdlib-only leaves —
     `products`, `paths`, `branding` — with their 266 test lines. The package
     owns the `PRODUCTS` table and `product_name`; `PATH_RULES` stays
     consumer-supplied (the c2c rule is meaningless in the API repo). The
     report-title hook moves from the API conftest into the package as an
     **opt-in** hook (the UI has none today — `pytest_html_report_title`
     appears nowhere in it, so "own it once" means *giving* the UI a title it
     lacks; decide that deliberately).
   - **2b (deferred):** `run_archive`, `run_report`, `report_media`,
     `health_summary`, `email_client` stay in the UI repo until a second
     consumer actually needs them. Before 2b starts it needs its own design
     pass: the exported API surface including the private names consumers
     already import (`_write_index`), the Playwright touchpoints, and an
     evidence type that carries what the renderer consumes (`TestRecord` has
     separate `screenshots`/`frames`/`traces`/`videos` plus `headline`,
     `signature`, `digest`, `seen`, `history` — the four-field `Evidence`
     sketch does not cover it).

3. **Jira** — `soterix_qa_core.jira`: extract `JiraClient` and FIX its latent
   bug (the `return {'key': ...}` sits inside `if story_link:`, so a real
   create with no story link returns None); package `QATouchToJiraMapper` and
   the discovery CLI with an injected mapping path. `multi_gate_validation`,
   the flaky DB and the CLI review loop stay in the UI repo.

4. **Shims + shim guards, then tag.** Module-alias shims per Rules 2-3, with
   the policy injection of Rule 8. Move the reporter/client unit tests into
   this package. Add `tests/plugins/test_shims.py` to EACH consuming repo — a
   broken shim is otherwise silent: replacing the alias with a star-import
   left the UI's own reporter suite fully GREEN while every monkeypatch seam
   stopped reaching the running code. The guard asserts module identity, that
   patching the old path reaches the package, and that writer paths stay
   inside the repo; the UI copy adds a test that the reviewed-gate policy is
   the one injected. **Then** bump `[project] version` and tag (Rule 4) — the
   tag must contain the reporter that step 5's shims import.

5. **Migrate the API repo** (4 consumers, subset tests, and unification fixes
   a live bug there). **5a** add the pinned dependency to `requirements.txt`,
   update `collect-gate.yml`'s header comment (it will no longer be
   credential-free), land it green alone. **5b** land the shims. Verify with
   one real QA Touch upload.

6. **Migrate the UI repo** (6a pin, 6b shims). The UI repo has **no
   merge-time gate at all** — its only workflow is `qa-nightly.yml` (canary
   paused since 2026-08-18, installing only `requests python-dotenv`; plus a
   workflow_dispatch smoke job). Add a UI collect gate as part of this
   migration, or accept that UI shim breakage is caught only by local runs.
   The canary must gain the package, base install only (it is not a pytest
   job, which is why `pytest` is an extra rather than a base dependency).

7. **Docs and interpreter range.** Both README Quickstarts, the merge
   runbook's contributor steps, and the credential setup for every QA team
   member (or publish to an index they can already reach). Reconcile the
   interpreter range: `pyproject` says `>=3.11`, the API gate runs 3.12 and
   UI dev runs 3.14 — pick one and state it.

## Guardrails to preserve

Mapping stays in config JSON, never test files. One run at a time.
Assertions read-only in repair. QA Touch upload dormant until armed; Jira
double-gated and approval-first. The archive's display-only mapping read
stays optional-by-contract (missing file = no case titles, no error).
