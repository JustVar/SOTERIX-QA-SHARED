# Rules and contract

Nine rules and one contract. Each was reproduced, not reasoned about. Break
one and the migration fails **quietly**. That is what they all have in
common, and why they are written down instead of left to judgement.

Read this before changing the packaging, the shims, or anything that writes
a file. `STATUS.md` says where the work stands; this says what must stay
true.

## Rules

### 1. One plugin registration name, ever

Listing both the shim path and the package path in `pytest_plugins` raises
`ValueError: Plugin already registered under a different name` before any
hook runs, so every session in both repos dies at configure time.

**So:** entry point OR shim, never both. The package ships no `[pytest11]`
entry point while consumers keep the shim.

### 2. Two shim shapes; the guarded one only where a CLI exists

A plain alias breaks any CLI: the module body swaps `sys.modules` and falls
off the end, so the command exits 0 having run nothing. Measured: through a
plain shim, `python tools/retry_uploads.py --dry-run` printed nothing and
exited 0 with a real pending failure in the log.

Modules **without** a `main()` must not use the guarded form; it raises
`AttributeError` on `python -m`.

| Module | Form |
|---|---|
| `retry_uploads`, later `run_report` | guarded |
| `qatouch_reporter`, `qatouch_client`, `products`, `paths`, `branding` | plain |

```python
import sys
try:
    from soterix_qa_shared.qatouch import reporter as _mod
except ModuleNotFoundError as exc:
    if (exc.name or "").split(".")[0] != "soterix_qa_shared":
        raise                    # a dependency of the package is missing
    raise ModuleNotFoundError(
        "soterix-qa-shared is not installed. "
        "Run: pip install -e ../soterix-qa-shared"
    ) from None
if __name__ == "__main__":       # guarded form only
    sys.exit(_mod.main())
sys.modules[__name__] = _mod
```

Two details are load-bearing:

- **The `exc.name` check.** Without it a missing *dependency of the package*
  is misreported as "the package is not installed". Real instance: under a
  base install, importing the reporter raises
  `ModuleNotFoundError(name='pytest')`, because `pytest` is an extra.
- **The `from ... import x as _mod` form.** The dotted
  `import soterix_qa_shared.qatouch.reporter` form makes a partial install
  (package present, submodule missing) misreport as "not installed".

### 3. Registration is unconditional; the failure is legible

Conditional registration was tested and rejected. With the package absent,
`pytest --qatouch-upload` exits 4 with `unrecognized arguments` and never
names the package, and `QATOUCH_UPLOAD=1` does nothing at all, a silent
wrong result.

**So:** the root conftest keeps `pytest_plugins` unconditional and relies on
rule 2's message, which pytest promotes to the last line on screen.

**Consequence to accept:** once a consumer migrates, this package becomes a
hard install dependency for any pytest run in that repo. Neither has
migrated yet. When they do: drop the "reporting is opt-in" framing from both
READMEs, and add the editable install to both Quickstarts and the merge
runbook.

A stale *installed* version reproduces the rejected mode exactly, and is
reachable through rule 4's silent no-op, so rule 4's guards are
prerequisites, not hygiene.

### 4. Every release tag must also bump `[project] version`

With the version unchanged, `pip install -r requirements.txt` against a new
tag pin is a silent exit-0 no-op that leaves the old code installed. `-U`
does not rescue it; `--force-reinstall` does.

**Two guards, both in step 0:** a CI job that fails when a pushed tag does
not equal `[project] version`, and a consumer-side assertion that
`direct_url.json`'s `requested_revision` matches the pin.

### 5. The package holds no repo-relative path

After installation `__file__` points into site-packages. Rootpath alone is
not enough either: two documented CLIs run with no pytest config at all.

**So:** the consumer passes a root to `configure(root)`. The plugin reads it from
`config.rootpath`, a CLI from the root it resolves itself. `layout.paths_for`
holds the single definition of what sits where.

`configure(root)` runs at the TOP of `pytest_configure`, before anything
decides whether to report, so a later hook or a CLI sharing the module sees
correct paths on any run. `--rootdir` silently relocates every derived path,
so the shim guard asserts the writer paths sit inside the repo.

### 6. Read module globals at call time

The path values stay monkeypatchable only if code reads the module global
when called. A recompute-on-use variant ignored both test patches and wrote
into the real repo, while the test that thought it was isolated still
passed.

### 7. Guard every write inside a pytest hook

An exception in `pytest_configure` aborts the whole session with
`INTERNALERROR`. Seen for real: a Windows 8.3-vs-long path made rootdir fall
back to `C:\Users` and the write raised `PermissionError`.

**So:** wrap writes in try/except and degrade to no reporting. Never compare
a rootpath to `Path.cwd()`/`resolve()` without normalising both.

### 8. The mapping policy is injected by the shim

Which mapping rows may upload is a per-repo decision, so the package holds no
opinion. **Both consumers now gate on `reviewed`.** The API repo did not, and
the measurement that changed it is worth keeping: of its 31 rows, the 13 the
matcher guessed all point at UI behaviour cases ("Verify tooltip on site
status" for an API status call), and all 7 of those cases appear in the live
QA Touch runs, while 0 of the 5 correctly-mapped ones do. Ungated, the only
results that would have landed were the wrong ones.

- **Channel:** the shim sets `_mod.MAPPING_POLICY = <callable>` *before* the
  `sys.modules` swap. Not a constructor argument, because the reporter is built
  inside `pytest_configure`, where a missing argument is an `INTERNALERROR`.
- **Contract:** `policy(path: Path) -> (mapping, counts)`. The counts keep the
  UI's "N of M rows are unreviewed" warning alive. A missing `reviewed` key
  means "not gated", never `KeyError`. Zero of the API's 31 rows carry one.
- **Failure:** no policy, or a policy that raises, resolves nothing and says
  so. Never a permissive fallback: an unreviewed guess writes a real status
  onto a case no test verifies.
- **Emit with plain `print()`.** `config.get_terminal_writer()` raises
  `AssertionError` inside `pytest_configure` (pytest 9.1.1), and
  `log.warning` is not reliably visible.
- **Scope:** the upload path only. The archive's display-only
  `load_case_mapping` keeps its permissive default. Two loaders with opposite
  policies is intended.

### 9. Declare `package-data`, and test the built wheel

An editable install's `.pth` masks missing data files. A CI job must install
the **built wheel** and import every subpackage. (It does not mask a missing
`__init__.py`. PEP 420 makes those importable either way.)

## The compatibility contract

**The shared mutable artifact is the QA Touch project, not the JSON files.**
`logs/upload_failures.json` and `logs/last_run_results.json` are per-repo and
gitignored, read only by the same repo's tools, *because* rules 5-7 keep
their writer paths inside the consumer repo. That containment is not free.

**One written path is MEANT to be tracked: `history/YYYY-MM.jsonl`.** It is
tracked in no repo yet, because no consumer has run an armed session through
the package. One append-only
line per armed run, ~350 bytes. It is the exception because it is the one
thing a run leaves that cannot be regenerated by re-running. Append only,
one file per month, and a merge conflict is always resolved by keeping both
lines in timestamp order. See [`RUN_HISTORY.md`](RUN_HISTORY.md).

**Results are written per test RUN, not per case.** The only status-writing
endpoint takes a run key, and the uploader refuses to register without one.
Two suites reporting the same `case_key` into different runs are isolated;
both results stand.

**One `case_key` should map to one test function.** The real hazard is
*within-repo* duplication. Measured: UI 42 keys claimed by more than one node
id (76 redundant claims), API 5 (19); collapsing parametrization, UI 38, API
4. The reporter handles this (worst status wins), but the mapping should
still be cleaned.

**Cross-repo:** exactly one shared key across 409 rows, `WEnvKQ` (API
`test_get_site_status`; UI `test_admin_sites.py::test_check_site_status`).
**Dormant on both sides now.** Both rows are unreviewed and both repos gate
on reviewed, and the API row is one of the 13 its matcher guessed. Measured
after the API gate landed: the set of case keys both repos would upload is
empty.

**The duplicate checker is a DESIGN, not a thing that exists.** No console
script, no CI job, nothing. When it is built: a console script taking N
mapping paths, each repo's CI running it over its own mapping. The
cross-repo pass needs a location holding both checkouts, so name that owner
before promising it. Fail only on a NEW duplicate versus a committed
baseline, and reuse the UI's existing `tools/qatouch_mapping_audit.py`
`shared` logic rather than writing a second one.

**Product model as it is today:** every mapped case is JlW1. `tests/ui/c2c/`
is tracked (7 files) and routed to 60W5 by `products.PATH_RULES`, but has no
mapping rows, so it cannot report. The uploader and the reporting layer
resolve product by different mechanisms. The reporter never imports
`products.py`. Unifying `PRODUCTS` does not unify resolution, and c2c uploads
would also need a per-project run key.

**Versioning:** record a minimum version per consumer, and note which package
version changed upload semantics.

## Guardrails to preserve

Mapping stays in config JSON, never in test files. One run at a time.
Assertions stay read-only during repair. QA Touch upload is dormant until
armed. Jira is double-gated and approval-first. The archive's display-only
mapping read stays optional by contract. A missing file means no case
titles, not an error.
