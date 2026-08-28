# soterix-qa-core — build status

Shared QA pipeline for the Soterix QA repos (Foundations plan step 1). Ends
the divergence of the duplicated QA Touch / Jira / reporting tooling: one
installable package both API and UI repos (and future product repos) depend
on, with a thin compatibility shim at each old import path.

## Done

- Package scaffold: src layout, `pyproject.toml` (name `soterix-qa-core`,
  optional extras `jira` and `dev`).
- **`soterix_qa_core.qatouch.client`** — the unified QA Touch client. Base is
  the UI implementation (the superset: `create_project`/`create_module` use
  the endpoints that actually exist; `update_test_run_results_by_code`,
  `verify_results_landed`, `update_test_case`, the legacy-field aliases, and
  the 8 status codes are all present). The API repo's
  `create_test_case_with_steps` is folded in. The `sys.path` bootstrap is
  dropped (package code never needs it). Ruff clean.
- Offline unit tests for the client helpers (`tests/test_client_helpers.py`).

## Next, in order (specs captured from the analysis workflows)

1. **Reporter** — `soterix_qa_core.qatouch.reporter`, base = UI
   `plugins/qatouch_reporter.py` (strict superset of the API copy). Changes
   from the raw copy:
   - Retarget the lazy `from tools.qatouch_client import QATouchClient` to
     `from soterix_qa_core.qatouch.client import QATouchClient`.
   - Paths (`MAPPING_PATH`, `FAILURE_LOG_DIR`, `RUN_RESULTS_PATH`) become
     repo-root-relative, resolved from pytest `config.rootpath` at
     `pytest_configure`, not from the package file location. The written
     files must land in the CONSUMING repo's `logs/`.
   - **The one true conflict — mapping-load policy.** UI uses a reviewed-gate
     loader (`tools/qatouch_mapping.reviewed_lookup`); the API mapping rows
     have no `reviewed` field, so the UI default would silently zero API
     uploads. Make the loader an injectable policy: default permissive
     (matches API today); the UI shim passes the reviewed-gate loader.
   - Bring `retry_uploads` along (UI-only today); freeze the
     `upload_failures.json` entry shape (retry_uploads parses it).
   - Two API-repo behaviors that unification silently FIXES — verify with one
     live API upload: the API run-key identifier and case_key fallback are the
     measured accepted-but-ignored forms, and the API playwright branch calls
     `update_playwright_results`, which exists in no client (AttributeError
     every run). The UI base routes both labels through
     `update_test_run_results_by_code`.
2. **Reporting stack** — `soterix_qa_core.reporting` (run archive, run.html,
   health, email). Make evidence agnostic: an `Evidence(label, kind, path|text,
   nodeid)` type with session-level and per-test collectors, so the UI
   registers a Playwright collector (screenshots/traces/video) and the API
   registers its masked HTTP-exchange text. Own the `PRODUCTS` map + report
   title hook once (the API conftest duplicates it verbatim). Keep the
   `archive -> html_report` import direction (avoids the documented cycle).
   Put Playwright-only rendering behind its own module so the API repo
   installs the package with no Playwright import path.
3. **Jira** — `soterix_qa_core.jira`: extract `JiraClient` from
   `smart_jira_bug_creator.py` (FIX the latent bug: the `return {'key': ...}`
   sits inside `if story_link:`, so a real create with no story link returns
   None); package `QATouchToJiraMapper` + the discovery CLI with an injected
   mapping path. Leave `multi_gate_validation`, the flaky DB, and the CLI
   review loop behind in the UI repo (they are run_suite orchestration).
4. **Shims (module-alias style, NOT `import *`).** Each repo keeps
   `plugins/qatouch_reporter.py`, `tools/qatouch_client.py`, etc. that end
   with `sys.modules[__name__] = <package module>` so the ~15 monkeypatch
   seams in both test suites still land. Move the reporter/client unit tests
   into this package; keep thin smoke coverage in each repo.
5. **Migrate the API repo first** (4 consumers, subset tests, lower risk),
   verify green, then the UI repo (20+ consumers, 537-line reporter suite).
6. **Install + CI.** Dev: editable install of the sibling checkout
   (`pip install -e ../soterix-qa-core`), documented in both READMEs — do NOT
   put a relative `-e` in requirements.txt. CI: create the package repo in
   Soterix-Systems (mirror to JustVar), read-only PAT under the same secret
   name in both orgs, pin `soterix-qa-core @ git+...@vX.Y.Z` in each
   requirements.txt. Two workflow edits are mandatory: the UI `qa-nightly`
   canary installs only `requests python-dotenv` today, and both collect
   gates install `requirements.txt` — both must gain the package. Support
   Python 3.12 (API CI) through 3.14 (UI).

## Guardrails to preserve

Mapping stays in config JSON, never test files. One run at a time.
Assertions read-only in repair. QA Touch upload dormant until armed; Jira
double-gated and approval-first. The mapping file is optional-by-contract
(missing file = no case titles, no error).
