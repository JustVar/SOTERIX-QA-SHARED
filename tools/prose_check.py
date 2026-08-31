#!/usr/bin/env python3
"""Check prose in this repo against docs/STYLE.md.

Usage:
  python tools/prose_check.py              # every tracked markdown file
  python tools/prose_check.py README.md    # named files
  python tools/prose_check.py --warnings   # include the advisory rules

Only the mechanical rules are checked. "Use active voice" and "one
instruction per sentence" need a reader, and a checker that guesses at them
produces noise that trains people to ignore it.

Code is exempt by rule, so fenced blocks and inline spans are stripped before
anything is counted. A rule about prose that fires on a shell command is a
rule people turn off.

Exit codes: 0 clean, 1 findings. Standard library only.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (name, pattern, message). Errors fail the check.
ERRORS = [
    ('dash', r'[—–]',
     'em or en dash. Use a period, a comma, or parentheses'),
    ('smart-quote', r'[‘’“”]',
     'smart quote. Use plain ASCII quotes'),
    ('exclamation', r'!(?!\[)(?!=)',
     'exclamation mark'),
    ('hype', (r'\b(seamless(?:ly)?|robust|powerful|cutting-edge|battle-tested|'
              r'comprehensive|intuitive|enterprise-grade|streamlines?|'
              r'leverages?|unlocks?)\b'),
     'hype word. Delete it or replace it with a measurement'),
    ('filler', (r'(it is important to note|please note|as mentioned above|'
                r'needless to say|\bessentially\b|\bbasically\b|\bsimply\b|'
                r'\bvery\b|in conclusion)'),
     'filler. Delete the phrase'),
]

# Real rules, but every repo violates them today, including docs/STYLE.md
# itself in its word-choice list. Reporting them as errors would fail the
# check everywhere on day one, so they are advisory until someone sweeps.
WARNINGS = [
    ('semicolon', r';', 'semicolon. Write two sentences'),
]


def strip_code(text: str) -> str:
    """Remove fenced blocks and inline spans, keeping line numbers intact."""
    def blank(match: re.Match) -> str:
        return re.sub(r'[^\n]', ' ', match.group(0))

    text = re.sub(r'```.*?```', blank, text, flags=re.DOTALL)
    text = re.sub(r'`[^`\n]*`', blank, text)
    # Link targets are paths, not prose.
    return re.sub(r'\]\([^)]*\)', blank, text)


def check(path: Path, rules: list[tuple[str, str, str]]) -> list[str]:
    findings = []
    text = strip_code(path.read_text(encoding='utf-8', errors='replace'))
    try:
        rel = path.relative_to(ROOT)
    except ValueError:
        rel = path                      # a file outside the repo, named directly
    for number, line in enumerate(text.splitlines(), start=1):
        for name, pattern, message in rules:
            for match in re.finditer(pattern, line, re.IGNORECASE):
                findings.append(
                    f'{rel}:{number}: {name}: {message}'
                    f'  ->  {line.strip()[:70]}')
    return findings


def tracked_markdown() -> list[Path]:
    try:
        out = subprocess.run(['git', 'ls-files', '*.md'], cwd=ROOT,
                             capture_output=True, text=True, timeout=30,
                             check=False)
        names = out.stdout.split()
    except (OSError, subprocess.SubprocessError):
        names = []
    if not names:                       # not a checkout, or no git
        return sorted(ROOT.rglob('*.md'))
    # These quote what they forbid. orwell.md is listed because the
    # style guide names it as the short version; it is not in this
    # repo yet, and a skip entry for an absent file is harmless.
    skip = {'docs/writing/ste.md', 'docs/writing/orwell.md', 'docs/STYLE.md'}
    return [ROOT / n for n in names if n not in skip]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('files', nargs='*', help='files to check')
    parser.add_argument('--warnings', action='store_true',
                        help='also report the advisory rules')
    args = parser.parse_args(argv)

    paths = [Path(f) for f in args.files] or tracked_markdown()
    paths = [p for p in paths if p.exists()]
    if not paths:
        print('No markdown files to check.')
        return 0

    errors: list[str] = []
    warnings: list[str] = []
    for path in paths:
        errors += check(path, ERRORS)
        if args.warnings:
            warnings += check(path, WARNINGS)

    for line in warnings:
        print(f'  warning: {line}')
    for line in errors:
        print(f'  {line}')

    print(f'\n{len(paths)} file(s) checked. '
          f'{len(errors)} finding(s)'
          + (f', {len(warnings)} warning(s)' if args.warnings else '')
          + '.')
    if errors:
        print('Rules: docs/STYLE.md. Full version: docs/writing/ste.md.')
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main())
