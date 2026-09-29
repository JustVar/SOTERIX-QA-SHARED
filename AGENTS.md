# Soterix QA Shared - Guidance for AI Coding Tools

If you use an AI tool to write, test, or update anything in this repo, point
it here first.

This is a library, not a test suite. Two repos install it as a built wheel
and run their whole test suite through it, so a quiet defect here becomes a
quiet defect everywhere at the next release. Read before making changes:

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
3. **`STATUS.md`** - what the package holds and how the two repos install it.
4. **`docs/RUN_HISTORY.md`** - what a test run leaves behind, and why exactly
   one written path is tracked in git.

## What This Repo Is Careful About

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

## Before You Commit

```bash
python -m pytest tests/ -q          # all offline: no network, no browser
python -m ruff check .
python tools/prose_check.py         # the mechanical rules from docs/STYLE.md
```

The prose check exists because these docs were written before the style
rules were wired into this repo, and 42 em dashes went in. Only the
mechanical rules are checked. Code is exempt, so fenced blocks, inline spans
and link targets are stripped before anything is counted.

Commit messages describe what the change does, in the repo's voice. No AI
attribution, no co-author lines, no model names.

## Verifying a Change

Unit tests are not enough for this package. Two defects reached a commit that
the suite could not see, and both were caught by *running* the thing:

- the shim guard template referenced three names that no longer existed, so
  it would have failed in a consumer for the wrong reason;
- the terminal summary read "Unmapped: 1 tests", in the line every QA person
  reads.

For anything touching the plugin, the shims or the CLIs, build the wheel,
install it into a scratch interpreter, and run a real pytest session against
it.
