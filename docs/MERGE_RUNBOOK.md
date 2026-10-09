# Merge and Release Runbook

How a change gets into this package, and how it reaches the two repos that
depend on it. The design rules a change must satisfy are in `docs/RULES.md`.
This file is only the process around them.

## The Remote

One remote, `origin`: github.com/JustVar/SOTERIX-QA-SHARED. Work on a
branch, open a pull request, and let CI gate the merge. Never force-push
`main`: its tags mark the wheels the consumers carry, and a rewritten tag
is a wheel nobody can rebuild.

## Making a Change

Work against a consumer, not against the unit tests alone. The unit tests
here have passed while a change still broke both repos, because what they
cannot see is the shim seam.

```powershell
# once: install this checkout into the consumer's venv, editable
cd ..\<webapp-qa checkout>
venv\Scripts\pip install -e ..\soterix-qa-shared
```

Editable means an edit here is live there with no reinstall, so the loop is:
change the package, run the consumer's suites, run this package's tests.

```powershell
cd ..\soterix-qa-shared
venv\Scripts\python -m pytest          # this package
venv\Scripts\python -m ruff check .
venv\Scripts\python tools\prose_check.py

cd ..\<webapp-qa checkout>            # and the consumer that will use it
venv\Scripts\python -m pytest tests\plugins tests\tools
venv\Scripts\python -m pytest tests\plugins\test_shims.py
```

Do not add `-q`. Both consumer repos already carry it in `pytest.ini`, and a
second one means `-qq`, which hides the pass count.

## Releasing

A consumer installs a built WHEEL from its own `vendor/` folder. Until a
new wheel is built and copied there, a merged change here reaches nobody.
Six steps, in this order:

1. **Bump `[project] version`** in `pyproject.toml`. Rule 4: a wheel whose
   version did not change installs as a silent exit-0 no-op that leaves the
   old code in place, and CI refuses a tag that does not equal the version.
2. **Tests, `ruff` and the prose check green.** Then commit and push.
3. **Wait for CI to pass on `main`.** CI runs on Linux and has caught bugs
   the Windows-only development machine could not: a path literal that was
   absolute on Windows and relative on Linux, and a shebang without an
   execute bit.
4. **Tag and push the tag.**

   ```powershell
   git tag -a v0.38.0 -m "v0.38.0 - what changed"
   git push origin v0.38.0
   ```

   The tag re-runs CI with the `tag-matches-version` job, which is skipped
   on ordinary pushes. Do not build the wheel until it is green.
5. **Build the wheel** from the tagged commit:

   ```powershell
   python -m pip wheel . --no-deps -w dist
   ```

   The file is `dist\soterix_qa_shared-0.38.0-py3-none-any.whl`.
6. **Copy the wheel into each consumer.** In each consumer's checkout:
   copy the wheel into `vendor/`, delete the old wheel, change the `==`
   version on the `soterix-qa-shared==` line of `requirements.txt`, run
   `pip install -r requirements.txt`, run the offline suites, commit and
   push. Confirm its CI is green.

Steps 5 and 6 are where a release goes wrong quietly. A wheel that is never
copied, or a version line that is never changed, leaves the consumer
installing the older package while `main` here looks finished. Each
consumer has a test that fails when its installed version, its `==` line
and the wheel in `vendor/` disagree.

## Adding a Layer

A new subpackage needs its `[project] description` and the package docstring
widened in the same commit, because rule 4 forces a version bump there
anyway and metadata that lags the contents misleads whoever reads it next.

New gates go in `soterix_qa_shared.gates` and take the repo root and their
own configuration as arguments. Rule 5: the package holds no repo-relative
path.

## After Any Release

- Both consumers carry the same wheel. Two repos on different versions is
  drift.
- `STATUS.md` still describes what the package holds.
- The local `ruff` matches the pin. A local version below CI's reports fewer
  findings and produces a green run that CI then fails.
