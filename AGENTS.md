# soterix-qa-shared - guidance for AI coding tools

If you use an AI tool to write, test, or update anything in this repo, point
it here first.

This is a library, not a test suite. Two repos install it and both run their
whole test suite through it, so a quiet defect here is a quiet defect
everywhere. Read before making changes:

1. **`docs/RULES.md`** - nine rules and the compatibility contract. Every one
   of them was reproduced, and every one of them prevents a *silent* failure:
   a run that reports success having uploaded nothing, a CLI that exits 0
   having run nothing, an install that leaves the old code in place. Read it
   before touching packaging, the shims, or anything that writes a file.
2. **`docs/writing/ste.md`** - the writing rules for all prose: docs,
   comments, docstrings, error messages, commit messages. Strict mode for
   error messages and log lines, flavored mode for docs. `docs/STYLE.md` is
   the short human summary. Never apply the rules to code, identifiers, or
   any name a reader must match against a screen.
3. **`STATUS.md`** - where the migration stands and what comes next.
4. **`docs/RUN_HISTORY.md`** - what a test run leaves behind, and why exactly
   one written path is tracked in git.

## What this repo is careful about

**A test that passes for the wrong reason.** Several tests here exist because
a mutation survived: the code was wrong and the suite stayed green. If you
add a test for a precedence rule, use the input ordering where the *wrong*
implementation gives a *different* answer - any other ordering proves
nothing.

**Reporting must never fail a run.** Every write inside a pytest hook is
guarded, and every sink is isolated from the others. An exception in
`pytest_configure` aborts the whole session with `INTERNALERROR`. The
blind-except clauses are deliberate and are silenced by file in `ruff.toml`
with the reason written down; do not narrow them to guessed exception types.

**No path constants.** After installation `__file__` points into
site-packages. Paths arrive from the consumer through `configure(root)`, and
`qatouch/layout.py` holds the single definition of what sits where.

**No opinion about which mapping rows may upload.** That is a per-repo
decision, injected by the consumer's shim. With no policy, nothing resolves
and the reporter says so - it never falls back to permissive.

## Before you commit

```bash
python -m pytest tests/ -q          # 143 tests, all offline
python -m ruff check .
```

Commit messages describe what the change does, in the repo's voice. No AI
attribution, no co-author lines, no model names.

## Verifying a change

Unit tests are not enough for this package. Two defects reached a commit that
the suite could not see, and both were caught by *running* the thing:

- the shim guard template referenced three names that no longer existed, so
  it would have failed in a consumer for the wrong reason;
- the terminal summary read "Unmapped: 1 tests", in the line every QA person
  reads.

For anything touching the plugin, the shims or the CLIs, build the wheel,
install it into a scratch interpreter, and run a real pytest session against
it.
