# Merge and release runbook

How a change gets into this package, and how it reaches the two repos that
depend on it. The design rules a change must satisfy are in `docs/RULES.md`.
This file is only the process around them.

## The two remotes

`origin` carries BOTH push URLs, personal and company, so a plain `git push`
updates both organisations at once:

```
company  https://github.com/Soterix-Systems/SOTERIX-QA-SHARED.git (push)
origin   https://github.com/JustVar/SOTERIX-QA-SHARED.git         (push)
origin   https://github.com/Soterix-Systems/SOTERIX-QA-SHARED.git (push)
```

A successful push prints `main -> main` twice. One line means only one
organisation moved, and the two have drifted.

`company` is the authority. Its CI gates every merge, and its tags are what
the consumers pin. The personal mirror is a backup.

## Making a change

Work against a consumer, not against the unit tests alone. The unit tests
here have passed while a change still broke both repos, because what they
cannot see is the shim seam.

```powershell
# once: install this checkout into the consumer's venv, editable
cd ..\playwright-soterix-qa
venv\Scripts\pip install -e ..\soterix-qa-shared
```

Editable means an edit here is live there with no reinstall, so the loop is:
change the package, run the consumer's suites, run this package's tests.

```powershell
cd ..\soterix-qa-shared
venv\Scripts\python -m pytest          # this package
venv\Scripts\python -m ruff check .
venv\Scripts\python tools\prose_check.py

cd ..\playwright-soterix-qa            # and the consumer that will use it
venv\Scripts\python -m pytest tests\plugins tests\tools
venv\Scripts\python -m pytest tests\plugins\test_shims.py
```

Do not add `-q`. Both consumer repos already carry it in `pytest.ini`, and a
second one means `-qq`, which hides the pass count.

## Releasing

A consumer pins a TAG. Until a tag exists and the consumer's pin moves, a
merged change here reaches nobody. Five steps, in this order:

1. **Bump `[project] version`** in `pyproject.toml`. Rule 4: a tag whose
   version did not change installs as a silent exit-0 no-op that leaves the
   old code in place, and CI refuses such a tag.
2. **Commit and push.** `main -> main` twice.
3. **Wait for CI to pass on `main`.** The matrix runs 3.11 to 3.14 and has
   caught bugs the Windows-only development machine could not: a path
   literal that was absolute on Windows and relative on Linux, and a
   shebang without an execute bit.
4. **Tag and push the tag to both remotes.**

   ```powershell
   git tag -a v0.20.0 -m "v0.20.0 - what changed"
   git push origin v0.20.0
   git push company v0.20.0
   ```

   The tag re-runs CI with the `tag-matches-version` job, which is skipped
   on ordinary pushes. Do not repin until it is green.
5. **Repin each consumer** in its `requirements-shared.txt`, commit, push,
   and confirm its CI is green against the new tag.

Steps 4 and 5 are where a release goes wrong quietly. A tag that is never
pushed, or a consumer that is never repinned, leaves CI installing an older
package while `main` here looks finished.

## Adding a layer

A new subpackage needs its `[project] description` and the package docstring
widened in the same commit, because rule 4 forces a version bump there
anyway and metadata that lags the contents misleads whoever reads it next.

New gates go in `soterix_qa_shared.gates` and take the repo root and their
own configuration as arguments. Rule 5: the package holds no repo-relative
path.

## After any release

- Both consumers pin the same tag. Two repos on different tags is drift.
- `STATUS.md` reflects what shipped, including the test count.
- The local `ruff` matches the pin. A local version below CI's reports fewer
  findings and produces a green run that CI then fails.
