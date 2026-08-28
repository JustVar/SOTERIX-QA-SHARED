"""Path fitting -- keeps every evidence file within Windows' path limit.

Windows refuses a file whose absolute path is longer than 260 characters
unless long paths are enabled (LongPathsEnabled in the registry, off by
default). The traces, videos, and screenshots a run writes carry names
derived from the test node id, so on a deep checkout (a long user name, a
nested folder) a save can cross that limit. Playwright then fails the
save, conftest prints the error, and the evidence is lost.

fit_name is the one place that decides a file name against the cap. A
consumer's conftest (traces, videos), its screenshot capture, and its
archive copies all call it, so a live write and its copy shorten the same
way and the report can match either name back to its test.

Nothing here reads a repo root or the environment: a directory and a stem
go in, a name comes out.

A shortened name keeps the stem's head, then an 8-char sha256 digest of
the full stem, then the stem's tail: <head>-<digest>-<tail><suffix>.
The digest keeps near-twin names distinct (parametrized camera ids
differ past the cut), and the tail keeps the part a human reads (the
camera id, the PASSED/FAILED label). Stdlib only.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

# Capping the absolute path at 250 leaves margin under Windows' 260-char
# MAX_PATH for the drive root and the terminating character.
PATH_MAX = 250

# A shortened stem keeps this many of its trailing chars: the tail is
# where near-twin names differ (parametrized camera ids, the
# PASSED/FAILED label).
FIT_TAIL = 26

# A repo root past this length leaves under 150 chars for reports/... plus
# the longest live name, so names start to shorten on every run.
LONG_ROOT = 100

DIGEST_CHARS = 8


def digest(stem: str) -> str:
    """The 8-char sha256 digest of a full stem that a shortened name carries.

    A consumer's report recomputes it from the expected name to match a
    shortened file back to its test, so the input must be the full stem
    (label and kind included, suffix excluded).
    """
    return hashlib.sha256(stem.encode('utf-8')).hexdigest()[:DIGEST_CHARS]


def fit_name(directory: Path, stem: str, suffix: str, limit: int = PATH_MAX) -> str:
    """The file name for `stem` + `suffix` whose absolute path under
    `directory` stays within `limit` characters.

    The untouched name when it fits. Else the stem is cut to
    <head>-<digest>-<tail>, so the name stays unique and recognisable.
    When the directory alone leaves no room for a head and a tail, the
    digest stands in for the whole stem: the write then fails on the
    OS's own message, or succeeds where long paths are enabled, instead
    of failing on an empty name.

    `directory` is measured as os.path.abspath measures it, not resolved:
    resolve() would expand 8.3 short names and measure a string other
    than the one the write uses.
    """
    parent = os.path.abspath(directory)
    room = limit - len(parent) - 1 - len(suffix)  # 1 for the separator
    if len(stem) <= room:
        return f'{stem}{suffix}'
    mark = digest(stem)
    keep = room - len(mark) - 2  # the two '-' joiners
    if keep < 2:
        return f'{mark}{suffix}'
    tail = min(FIT_TAIL, keep // 2)
    return f'{stem[:keep - tail]}-{mark}-{stem[-tail:]}{suffix}'


def long_root_warning(root: Path, threshold: int = LONG_ROOT) -> str | None:
    """The one-line warning a consumer CLI prints for a deep checkout, or None."""
    length = len(os.path.abspath(root))
    if length <= threshold:
        return None
    return (f'Repo path is {length} characters. Long test names may hit '
            f"Windows' 260-char limit. Enable long paths or clone nearer "
            f'the drive root.')
