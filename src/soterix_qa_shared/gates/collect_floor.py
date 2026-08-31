"""Fail when a suite collects fewer tests than it used to.

pytest exits 5 when it collects nothing, so a gate that runs pytest already
catches a suite that vanishes. Nothing catches one that SHRINKS. A path typo
that collects `tests/plugins` instead of `tests/plugins tests/tools` drops
597 tests to 300, exits 0, and the run is green.

That is not hypothetical. Tests have gone missing three times in these
repos. A `norecursedirs` entry matched basenames and silently skipped
tests/tools/, so a set of client regression pins ran green-by-absence for a
day. A merge from the company remote reverted files that nothing conflicted
with. The shared-package extraction moved hundreds of tests between
repositories. Every time, the collected count was the only evidence, and
nothing was watching it.

The count is easy to lose sight of, because both consumer repos carry `-q`
in their pytest.ini. A command that adds another `-q` gets `-qq`, which
suppresses the summary line entirely. Never add `-q` to a pytest command in
a repo whose pytest.ini already has one.

A consumer wires this up with a small script that supplies its own suites:

    from soterix_qa_shared.gates import collect_floor
    SUITES = {'whole repo': [], 'offline suites': ['tests/plugins']}
    sys.exit(collect_floor.main(sys.argv[1:], root=ROOT, suites=SUITES))

Standard library only, so the gate runs before anything else is installed.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

COUNT_RE = re.compile(r'(\d+) tests? collected')

#: Exit codes. 0 clean, 1 a suite shrank, 2 the check could not be made.
OK, SHRANK, ERROR = 0, 1, 2


def collected(root: Path, target: list[str],
              timeout: int = 600) -> tuple[int, str | None]:
    """How many tests pytest collects for a target, and any error.

    An error is returned rather than a count of zero. A zero that means "the
    check failed" and a zero that means "there are no tests" have to be told
    apart, because --update would otherwise write the failure in as the new
    floor and the gate would protect nothing from then on.
    """
    try:
        proc = subprocess.run(
            [sys.executable, '-m', 'pytest', '--collect-only', *target],
            cwd=str(root), capture_output=True, text=True, timeout=timeout,
            check=False)
    except subprocess.TimeoutExpired:
        return 0, f'pytest did not finish collecting within {timeout}s'
    except (OSError, subprocess.SubprocessError) as exc:
        return 0, f'could not run pytest: {exc}'
    # A collection error still prints a count for what did collect, so the
    # exit code is what says the number is trustworthy. 0 is a clean
    # collection. 5 is "no tests collected", which is a real answer of zero.
    if proc.returncode not in (0, 5):
        tail = (proc.stdout or proc.stderr or '')[-300:]
        return 0, f'pytest exited {proc.returncode}: {tail.strip()}'
    found = COUNT_RE.search((proc.stdout or '').replace('\r', '\n'))
    if not found:
        return (0, None) if proc.returncode == 5 else (
            0, 'pytest printed no "N tests collected" line')
    return int(found.group(1)), None


def load_floor(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    suites = data.get('suites')
    return suites if isinstance(suites, dict) else {}


def save_floor(path: Path, counts: dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        {
            'comment': 'Lowest acceptable collected-test count per suite. '
                       'Higher is always fine. Lower needs a reason in the '
                       'commit message. See soterix_qa_shared.gates.'
                       'collect_floor.',
            'suites': dict(sorted(counts.items())),
        },
        indent=2) + '\n', encoding='utf-8')


def main(argv: list[str] | None = None, *, root: Path,
         suites: dict[str, list[str]], floor_file: Path | None = None) -> int:
    """Run the gate. `suites` maps a display name to a pytest target list."""
    parser = argparse.ArgumentParser(
        description='Fail when a suite collects fewer tests than it used to.')
    parser.add_argument('--update', action='store_true',
                        help='record the current counts as the new floor')
    args = parser.parse_args(argv)

    path = floor_file or root / 'config' / 'collect_floor.json'

    counts: dict[str, int] = {}
    for name, target in suites.items():
        found, error = collected(root, target)
        if error:
            print(f'collect floor: {name}: {error}')
            return ERROR
        counts[name] = found

    if args.update:
        save_floor(path, counts)
        for name, n in sorted(counts.items()):
            print(f'  {name}: {n}')
        print(f'Recorded in {path.name}.')
        return OK

    floor = load_floor(path)
    if not floor:
        print(f'No floor recorded in {path.name}. Run this with --update.')
        return ERROR

    shrunk = {name: (floor[name], counts.get(name, 0))
              for name in floor if counts.get(name, 0) < floor[name]}
    for name, (was, now) in sorted(shrunk.items()):
        print(f'  {name}: {was} -> {now} tests collected ({was - now} missing)')
    if shrunk:
        print(f'\n{len(shrunk)} suite(s) collect fewer tests than the floor. '
              f'If the removal was deliberate, say why in the commit message '
              f'and run this with --update.')
        return SHRANK

    grew = {name: (floor[name], counts[name])
            for name in floor if counts.get(name, 0) > floor[name]}
    for name, (was, now) in sorted(grew.items()):
        print(f'  {name}: {was} -> {now} tests collected (+{now - was})')
    if grew:
        print('Run this with --update to lock the higher count in.')
    print('  ' + ', '.join(f'{name}: {n}' for name, n in sorted(counts.items())))
    print('No suite shrank.')
    return OK
