"""Unit tests for soterix_qa_shared.reporting.paths.

fit_name against real directories: a name that fits passes through, an
overlong name shortens to <head>-<digest>-<tail> exactly at the cap, a
relative directory is measured as absolute, near-twin stems get distinct
names, the tail survives the cut, a directory with no room falls back to
the digest alone. Plus the long-root warning run.py prints. No browser,
no network.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from soterix_qa_shared.reporting import paths

DIGEST_RE = re.compile(r'-([0-9a-f]{8})-')


def _limit(directory: Path, stem_room: int, suffix: str = '.png') -> int:
    """A cap that leaves exactly `stem_room` chars for the stem."""
    return len(os.path.abspath(directory)) + 1 + stem_room + len(suffix)


def _length(directory: Path, name: str) -> int:
    return len(os.path.abspath(directory / name))


# ---------------------------------------------------------------------------
# fit_name
# ---------------------------------------------------------------------------

def test_fit_name_passes_a_fitting_name_through(tmp_path):
    """A name whose absolute path sits at or under the cap is untouched."""
    limit = _limit(tmp_path, len('a__PASSED'))
    assert paths.fit_name(tmp_path, 'a__PASSED', '.png', limit) == 'a__PASSED.png'


def test_fit_name_measures_a_relative_directory_as_absolute():
    """A consumer passes a relative directory. The cap is on the absolute
    path the write actually uses."""
    directory = Path('reports') / 'artifacts'
    limit = _limit(directory, 10, '.zip')
    assert paths.fit_name(directory, 'x' * 10, '.zip', limit) == 'x' * 10 + '.zip'
    assert paths.fit_name(directory, 'x' * 11, '.zip', limit) != 'x' * 11 + '.zip'


def test_fit_name_shortens_to_the_cap_with_the_full_stem_digest(tmp_path):
    """An overlong stem is cut to <head>-<digest>-<tail>, landing exactly
    on the cap with its suffix kept. The digest is of the whole stem: that
    is what a consumer's report recomputes to match the file back to
    its test."""
    stem = 't' * 300
    limit = _limit(tmp_path, 60, '.zip')
    name = paths.fit_name(tmp_path, stem, '.zip', limit)
    assert _length(tmp_path, name) == limit
    assert name.endswith('.zip')
    found = DIGEST_RE.search(name)
    assert found is not None
    assert found.group(1) == paths.digest(stem)


def test_fit_name_default_cap_is_250(tmp_path):
    """Without a limit the cap is 250, the margin under Windows' 260."""
    assert paths.PATH_MAX == 250
    name = paths.fit_name(tmp_path, 'y' * 300, '.webm')
    assert _length(tmp_path, name) == 250


def test_fit_name_keeps_near_twins_distinct(tmp_path):
    """Stems that differ only in the middle must not fit to one name: the
    later write would silently overwrite the earlier test's evidence."""
    limit = _limit(tmp_path, 40, '.zip')
    twins = [paths.fit_name(tmp_path, f'{"h" * 30}cam-{i}{"t" * 30}', '.zip', limit)
             for i in (3, 4)]
    assert twins[0] != twins[1]
    for name in twins:
        assert _length(tmp_path, name) <= limit


def test_fit_name_keeps_the_stem_tail(tmp_path):
    """The tail is what tells near-twins apart to a human - the camera id,
    the PASSED/FAILED label - so the cut must not eat it."""
    name = paths.fit_name(tmp_path, 'x' * 290 + '__FAILED', '.png', _limit(tmp_path, 50))
    assert name.endswith('__FAILED.png')
    assert name.startswith('x' * 10)


def test_fit_name_falls_back_to_the_digest_when_the_folder_leaves_no_room(tmp_path):
    """A folder past the cap cannot hold a head and a tail. The digest
    alone still names the file, so a write under enabled long paths
    lands somewhere findable instead of failing on an empty name."""
    deep = tmp_path / ('d' * 300)
    stem = 'tests-ui-x.py-test_t-trace'
    assert paths.fit_name(deep, stem, '.zip') == f'{paths.digest(stem)}.zip'


# ---------------------------------------------------------------------------
# Long-root warning (a consumer CLI prints it once at startup)
# ---------------------------------------------------------------------------

def _root_of_length(total):
    """An ABSOLUTE path whose abspath is exactly `total` characters.

    Built from the filesystem root rather than a literal like Path('C:/')
    plus padding.
    That literal is 101 characters on Windows and a RELATIVE path on Linux,
    where abspath resolves it against the working directory and the length
    depends on where CI checked the repo out. CI on Linux caught
    it on its first run: 101 intended, 154 measured.

    Not pytest's tmp_path either: that carries the test name and is already
    about 100 characters, so there is no room left to pad down to the limit.
    """
    root = Path(os.path.abspath(os.sep))          # 'C:\' or '/'
    candidate = root / 'r'
    while len(os.path.abspath(candidate)) < total:
        candidate = root / ('r' * (len(candidate.name) + 1))
    assert len(os.path.abspath(candidate)) == total, (
        f'could not build a {total}-char path from {root}')
    return candidate


def test_long_root_warning_is_silent_for_a_short_root():
    """A root at or under 100 characters gets no warning."""
    assert paths.LONG_ROOT == 100
    root = _root_of_length(100)
    assert len(os.path.abspath(root)) == 100
    assert paths.long_root_warning(root) is None


def test_long_root_warning_fires_one_character_past_the_limit():
    """The boundary, in both directions, so the comparison cannot drift."""
    assert paths.long_root_warning(_root_of_length(101)) is not None


def test_long_root_warning_names_the_length_and_the_fixes():
    """A root past 100 characters gets one line with the length, the
    limit, and both fixes."""
    root = _root_of_length(140)
    text = paths.long_root_warning(root)
    assert text is not None
    assert '\n' not in text
    assert 'Repo path is 140 characters' in text
    assert '260' in text
    assert 'long paths' in text
    assert 'drive root' in text
