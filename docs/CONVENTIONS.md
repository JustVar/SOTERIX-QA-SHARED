# Conventions

How code gets written in this package. `docs/RULES.md` says what must stay
true about the architecture. This says how to work on it.

Every convention below is here because breaking it cost something real. The
incident is named in each case, because a rule with no reason attached is a
rule the next person deletes.

## Testing

**A test is believed once a mutation kills it.** Break the code the test
covers, deliberately, and confirm the test fails. A test that passes against
broken code is documentation, not a guard.

This is not theoretical. A cap on a printed list was covered by a test
asserting the "+194 more" counter. Removing the cap entirely left the
counter intact, so the test still passed while the feature was gone. The
test now asserts the count of codes on the line as well.

Mutate the specific behaviour, not a random line. Useful mutations: invert a
comparison, make a condition always false, return early, drop a clause from
a message, remove a guard.

**A gate must fail in both directions.** Prove it passes when it should and
fails when it should. A gate only ever seen passing is indistinguishable
from no gate. The collected-test floor has tests for a suite that shrank, a
suite that grew, an absent floor file, and a collection that could not run.

**Do not assert from reading.** Measure. A claim that two modules had
identical public surfaces was produced by comparing their ASTs and was wrong
in twelve places, because annotated assignments parse differently. Importing
both and diffing `dir()` gave the truth.

## Writing the code

**Read module globals at call time**, so a consumer's `monkeypatch` reaches
the running code. This is rule 6 in `docs/RULES.md` and it is the seam the
shims depend on.

**Never record a failure as a value.** A count that could not be taken is an
error, not a zero. `collected()` returns `(0, error)` rather than `0`,
because `--update` would otherwise write the failed run in as the new floor
and disarm the gate for good.

**Say what is uncovered, not only what succeeded.** An upload reported four
results landed and every check green, over a run that still held an untested
case. "Did my write land" and "is the run finished" are different questions.
Answer both.

**Empty is a special case, and usually means do nothing.** A dry pass with
no results overwrote the previous run's record with an empty one, in fifteen
of twenty one pytest invocation modes. The sink now refuses to write when it
has nothing.

## Docstrings

Say why, and cite the measurement. A docstring that restates the function
name is noise. These are the ones worth writing:

```python
def relative_target(args, root):
    """pytest's positional args, rewritten relative to the repo root.

    Stored verbatim, the same full-suite run records `tests` for one
    developer and `C:/Users/<name>/.../tests` for another, which breaks
    every query that groups by target and writes somebody's home directory
    into a shared file.
    """
```

Numbers in prose go stale. Write the number only where it is measured, or
say where to read it. A docstring here once quoted a lint total that had
already moved.

## Running the tests

```bash
python -m pytest
python -m ruff check .
python tools/prose_check.py
```

**Never add `-q`.** Both consumer repos carry it in `pytest.ini`. A second
one makes `-qq`, which suppresses the summary line, so a run that collected
twelve tests and one that collected six hundred look identical.

**Match the pinned `ruff`.** A local version below CI's reports fewer
findings and produces a green local run that CI then fails. This has
happened twice, at 0.16.1 against 0.16.2 and at 0.16.2 against 0.16.5.

**Prose follows `docs/STYLE.md`**, checked mechanically by
`tools/prose_check.py`. No em dashes, no smart quotes, no exclamation marks,
no hype words.

## Cross-platform

Development is on Windows and CI is on Linux, so anything touching a path
is suspect until the matrix has run it.

`Path('C:/')` is an absolute 101-character path on Windows and a RELATIVE
path on Linux, where it resolved against the checkout directory and measured
154. Build from `os.path.abspath(os.sep)` instead. A shebang with no execute
bit is a lint finding on Linux and invisible on Windows.

Record paths as posix, and relative to a root, whenever they are written to
a file that other people read.

## Before opening a pull request

- The package's tests, `ruff` and the prose check pass.
- At least one consumer's suites and shim guards pass against the edit,
  installed editable. The unit tests here cannot see the shim seam.
- Anything new that can fail silently has a test that proves it fails.
- `docs/MERGE_RUNBOOK.md` covers the release steps after the merge.
