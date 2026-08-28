# soterix-qa-core — build status

Shared QA pipeline for the Soterix QA repos (Foundations plan step 1). Ends
the divergence of the duplicated QA Touch / Jira / reporting tooling: one
installable package that the API repo, the UI repo, and future product repos
(Mobile, C2C, Neurosen) depend on, with a thin compatibility shim at each old
import path.

Every mechanism below was proven by prototype in an adversarial audit
(2026-08-27, 19 agents). Claims that failed that audit are recorded here as
rules, not as prose to be rediscovered later.

## Done

- Package scaffold: src layout, `pyproject.toml` (setuptools, extras `jira`
  and `dev`). Wheel build verified: `soterix_qa_core.qatouch` ships correctly.
- **`soterix_qa_core.qatouch.client`** — the unified QA Touch client. Base is
  the UI implementation (the superset and the corrected one: its
  `create_project`/`create_module` use the endpoints that exist, and it
  carries `update_test_run_results_by_code`, `verify_results_landed`,
  `update_test_case`, the legacy-field aliases and all eight status codes).
  The API repo's `create_test_case_with_steps` is folded in. The `sys.path`
  bootstrap is dropped. Ruff clean.
- Offline unit tests for the client helpers (`tests/test_client_helpers.py`).

## Rules the audit turned up (violate these and the migration breaks quietly)

1. **One plugin registration name, ever.** Listing both the shim path
   (`plugins.qatouch_reporter`) and the package path in `pytest_plugins`
   raises `ValueError: Plugin already registered under a different name`
   before any hook runs — pluggy dedupes on name but rejects the same object
   under a second name, and the alias guarantees one object. Corollary: the
   package must NOT ship a `[pytest11]` entry point while consumers keep the
   shim in `pytest_plugins`, or every run in both repos dies at configure
   time. Choose entry point OR shim, never both.
2. **The shim template must put the `__main__` guard BEFORE the alias swap.**
   `python -m plugins.<mod>` through a plain alias shim is a silent no-op
   (exit 0, no output): runpy execs the shim as `__main__`, the package
   module imports under its own name, its guard never fires. The UI repo has
   68 `tools/*.py` with `__main__` guards and four documented `python -m`
   invocations. Required shape:

   ```python
   import sys
   from soterix_qa_core.qatouch import reporter as _mod   # legible failure below
   if __name__ == "__main__":
       sys.exit(_mod.main())
   sys.modules[__name__] = _mod
   ```

   The package module must expose a real `main()`; an inline
   `if __name__ == "__main__":` block cannot be called.
3. **The shim must fail legibly.** Without the package installed, the root
   conftest's `pytest_plugins` hard-blocks EVERY run in the repo (143 lines
   of traceback, and the word "pip install" appears zero times). Wrap the
   import: `except ModuleNotFoundError: raise ModuleNotFoundError(
   "soterix-qa-core is not installed. Run: pip install -e ../soterix-qa-core")`.
4. **Decide whether QA Touch stays optional.** Both READMEs promise reporting
   is opt-in, but a missing package blocks a plain `pytest -m smoke` for the
   whole QA team. Either register the plugin conditionally (only when the
   package imports) or drop the "opt-in" framing from both READMEs.
5. **Every release tag MUST also bump `[project] version`.** Reproduced: with
   the version left alone, `pip install -r requirements.txt` against a new
   tag pin is a silent exit-0 no-op that leaves the old code installed, and
   `-U` does not rescue it. Add a CI guard that asserts the installed version
   or `direct_url.json` commit after install.
6. **Paths: read module globals at call time.** The rootpath-derived
   constants stay monkeypatchable only if functions read the module global
   when called; a recompute-from-ROOTPATH variant silently ignored both test
   patches and wrote into the real repo's `logs/`.
7. **Guard every write in a pytest hook.** `config.rootpath` lands inside the
   repo only because each consumer keeps a root `pytest.ini` (both do today).
   Without one, rootdir escapes to the parent — and in one Windows 8.3-vs-long
   path case it fell back to `C:\Users` and killed the session with
   `INTERNALERROR PermissionError`, because an exception in `pytest_configure`
   aborts the entire run. Wrap writes in try/except and degrade to
   no-reporting. Never compare `rootpath` to `Path.cwd()`/`resolve()` without
   normalizing both: rootpath is not normalized.
8. **The mapping-load policy argument is REQUIRED, no default.** A consumer
   that omits it must fail at import rather than silently uploading
   unreviewed guesses. UI passes `reviewed_lookup`; API passes an explicit
   permissive loader.
9. **Declare `package-data`** for any non-.py file the package needs
   (`[tool.setuptools.package-data]`), and add a CI step that installs the
   BUILT WHEEL and imports every subpackage — the editable install's `.pth`
   masks both missing data files and missing `__init__.py`.

## The compatibility contract (was implicit, now explicit)

- **The shared mutable artifact is the QA Touch project, not the JSON files.**
  `logs/upload_failures.json` and `logs/last_run_results.json` are per-repo,
  gitignored, and read only by the same repo's tools. Version skew between
  repos is therefore safe for those files.
- **`case_key` ownership is exclusive.** Both repos' tracked mappings declare
  project `JlW1`, and they already collide on one live case:
  **`WEnvKQ`** is claimed by the API repo
  (`test_dashboard_apis.py::test_get_site_status`,
  `test_platform_apis.py::test_get_site_status`) and by a UI admin test.
  Whichever suite runs second overwrites the other's result. **Resolve this
  before wiring cross-repo uploads**, and add a cross-repo check that fails
  when a `case_key` appears in more than one repo's mapping.
- Record a minimum-core-version per consumer, and note here which core
  version changed upload semantics, so a v0.1/v0.3 pair is a documented
  decision rather than an accident.

## Next, in order

**0. Create the package repo and push it — before anything is built on top.**
Steps 1-3 otherwise pile the whole extraction onto a local checkout with no
remote and no backup, on a machine whose CLAUDE.md records a laptop wipe.
Match the verified workspace shape (not "mirror to JustVar"):
`gh repo create` in Soterix-Systems, `origin` = JustVar carrying BOTH push
URLs, `company` = Soterix-Systems fetch-only. Copy the governance files that
do not travel: `AGENTS.md`, `README.md`, `ruff.toml`, `docs/writing/ste.md`,
`docs/STYLE.md`, a merge runbook. Add a `MEMORY.md` pointer line and run
`~/.claude/memory_lint.py`. Add the package's own collect-gate CI (it has
none). Tag `v0.1.0`.

1. **Reporter** — `soterix_qa_core.qatouch.reporter`, base = UI
   `plugins/qatouch_reporter.py` (a strict superset of the API copy). Retarget
   the lazy client import to the package; make paths rootpath-derived under
   rules 6 and 7; make the mapping loader a required policy argument (rule 8);
   bring `retry_uploads` along and freeze the `upload_failures.json` entry
   shape it parses.
   Two API-repo behaviors that unification fixes, to verify with one live
   upload: its run-key identifier and case_key fallback are the measured
   accepted-but-ignored forms, and its playwright branch calls
   `update_playwright_results`, which exists in no client. That branch is
   opt-in via `QATOUCH_USE_PLAYWRIGHT_ENDPOINT` and the AttributeError is
   swallowed by `_upload_with_retry`, so today's symptom is three retries then
   a logged error and a zero-upload run, not a crash.
2. **Reporting stack** — `soterix_qa_core.reporting` (run archive, run.html,
   health, email). Evidence becomes agnostic: an `Evidence(label, kind,
   path|text, nodeid)` type with session-level and per-test collectors, so the
   UI registers a Playwright collector (screenshots/traces/video) and the API
   registers its masked HTTP-exchange text. The package owns the `PRODUCTS`
   map and the report-title hook once (the API conftest duplicates it
   verbatim). Keep the `archive -> html_report` import direction (the
   documented cycle). Playwright-only rendering sits behind its own module so
   the API repo installs with no Playwright import path.
3. **Jira** — `soterix_qa_core.jira`: extract `JiraClient` from
   `smart_jira_bug_creator.py` and FIX its latent bug (the
   `return {'key': ...}` sits inside `if story_link:`, so a real create with
   no story link returns None); package `QATouchToJiraMapper` and the
   discovery CLI with an injected mapping path. `multi_gate_validation`, the
   flaky DB and the CLI review loop stay in the UI repo (run_suite
   orchestration).
4. **Shims + shim guards.** Module-alias shims per rules 1-3. Move the
   reporter/client unit tests into this package. Add `tests/plugins/
   test_shims.py` to EACH consuming repo — a broken shim is otherwise
   silent: replacing the alias with a star-import left the UI's own 11-test
   reporter suite fully GREEN while every monkeypatch seam silently stopped
   reaching the running code. The guard asserts module identity, that
   patching the old path reaches the package, and that the writer paths stay
   inside the repo. The UI copy adds a fifth test asserting the reviewed-gate
   loader is the one injected.
5. **Migrate the API repo** (4 consumers, subset tests, and unification fixes
   a live bug there). Split the landing so a failure is isolable:
   **5a** add the pinned `soterix-qa-core @ git+...@v0.1.0` to
   `requirements.txt` and land it green on its own PR; **5b** then land the
   shims. Verify with one real QA Touch upload.
6. **Migrate the UI repo** the same way (6a pin, 6b shims). Note the real
   risk the audit surfaced: **the UI repo has no merge-time gate at all** —
   its only workflow is `qa-nightly.yml` (canary, schedule paused since
   2026-08-18, installs only `requests python-dotenv`; plus a
   workflow_dispatch smoke job). Add a UI collect gate as part of this
   migration, or accept that UI shim breakage is caught only by local runs.
   The canary's install step must gain the package either way.
7. **Install + docs.** Dev: `pip install -e ../soterix-qa-core` (writes a
   gitignored `src/*.egg-info/`); never a relative `-e` in requirements.txt.
   CI: read-only PAT under the same secret name in both orgs. Update both
   README Quickstarts and MERGE_RUNBOOK contributor steps to clone both repos
   and run the editable install; if the pin stays a private git URL, document
   the credential setup for every QA team member or publish to an index they
   can already reach. Support Python 3.12 (API CI) through 3.14 (UI dev); add
   a CI matrix job asserting shim identity per interpreter, since the alias
   mechanism is CPython import behavior rather than a language guarantee.

## Guardrails to preserve

Mapping stays in config JSON, never test files. One run at a time.
Assertions read-only in repair. QA Touch upload dormant until armed; Jira
double-gated and approval-first. The mapping file is optional-by-contract
(missing file = no case titles, no error).
