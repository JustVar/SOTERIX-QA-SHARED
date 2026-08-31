"""The collected-test floor.

A gate that only ever passes is indistinguishable from no gate, so these
drive it in both directions: a suite that shrank must fail, a suite that
grew must pass and say so. Collection itself is stubbed in the comparison
tests. Really collecting a suite would make this a slow unit test, and what
is under test is the comparison. The `collected` tests below do run pytest,
against a directory the test writes, so the parsing is checked against the
real thing rather than a fixture of remembered output.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from soterix_qa_shared.gates import collect_floor as cf


@pytest.fixture
def floor(tmp_path):
    return tmp_path / 'collect_floor.json'


def _run(monkeypatch, floor, mapping, argv=()):
    """Run main with each suite reporting the count this mapping gives it."""
    counts = iter(mapping.values())
    monkeypatch.setattr(cf, 'collected', lambda root, target: (next(counts), None))
    return cf.main(list(argv), root=Path('.'),
                   suites={name: [] for name in mapping}, floor_file=floor)


def _write(floor, suites):
    floor.write_text(json.dumps({'suites': suites}), encoding='utf-8')


# ---------------------------------------------------------------------------
# The comparison
# ---------------------------------------------------------------------------

def test_a_shrunken_suite_fails(floor, monkeypatch, capsys):
    """The whole point. 597 -> 580 must not be a green run."""
    _write(floor, {'offline': 597})
    assert _run(monkeypatch, floor, {'offline': 580}) == cf.SHRANK
    out = capsys.readouterr().out
    assert '597 -> 580' in out
    assert '17 missing' in out


def test_an_unchanged_suite_passes(floor, monkeypatch):
    _write(floor, {'offline': 597})
    assert _run(monkeypatch, floor, {'offline': 597}) == cf.OK


def test_one_shrunken_suite_fails_the_run_even_when_another_grew(floor, monkeypatch):
    """Otherwise a change that moves tests between suites nets out to green
    while one suite quietly lost coverage."""
    _write(floor, {'offline': 597, 'whole repo': 1416})
    assert _run(monkeypatch, floor,
                {'offline': 580, 'whole repo': 1500}) == cf.SHRANK


def test_a_grown_suite_passes_and_asks_for_an_update(floor, monkeypatch, capsys):
    """Growth is always fine, but a floor left un-raised protects the old
    number rather than the new one, so the run says so."""
    _write(floor, {'offline': 597})
    assert _run(monkeypatch, floor, {'offline': 620}) == cf.OK
    assert '+23' in capsys.readouterr().out


def test_a_floor_entry_with_no_suite_fails(floor, monkeypatch, capsys):
    """A renamed suite leaves its old name in the floor at a real count.
    Reading that as zero and failing is right: a rename should be
    deliberate, not silent."""
    _write(floor, {'renamed away': 100})
    assert _run(monkeypatch, floor, {'offline': 597}) == cf.SHRANK
    assert 'renamed away' in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Refusing to guess
# ---------------------------------------------------------------------------

def test_no_floor_recorded_is_an_error_not_a_pass(floor, monkeypatch, capsys):
    """An absent floor file must not read as 'nothing shrank'."""
    assert _run(monkeypatch, floor, {'offline': 597}) == cf.ERROR
    assert '--update' in capsys.readouterr().out


@pytest.mark.parametrize('body', ['not json at all', '[]', '{}', '{"suites": 3}'])
def test_an_unusable_floor_file_is_an_error_not_a_pass(floor, monkeypatch, body):
    """A truncated or hand-edited file must fail the gate, not disarm it."""
    floor.write_text(body, encoding='utf-8')
    assert _run(monkeypatch, floor, {'offline': 597}) == cf.ERROR


def test_a_failing_collection_is_an_error_not_a_zero(floor, monkeypatch, capsys):
    _write(floor, {'offline': 597})
    monkeypatch.setattr(cf, 'collected', lambda root, target: (0, 'pytest exited 2'))
    assert cf.main([], root=Path('.'), suites={'offline': []},
                   floor_file=floor) == cf.ERROR
    assert 'pytest exited 2' in capsys.readouterr().out


def test_update_after_a_failed_collection_writes_nothing(floor, monkeypatch):
    """Otherwise one broken run lowers the floor to zero and the gate
    protects nothing from then on."""
    _write(floor, {'offline': 597})
    monkeypatch.setattr(cf, 'collected', lambda root, target: (0, 'no pytest'))
    assert cf.main(['--update'], root=Path('.'), suites={'offline': []},
                   floor_file=floor) == cf.ERROR
    assert json.loads(floor.read_text())['suites'] == {'offline': 597}


def test_update_writes_the_current_counts(floor, monkeypatch):
    assert _run(monkeypatch, floor, {'offline': 601}, argv=['--update']) == cf.OK
    assert json.loads(floor.read_text())['suites'] == {'offline': 601}


# ---------------------------------------------------------------------------
# Counting, against a real pytest run
# ---------------------------------------------------------------------------

def _suite(tmp_path, how_many):
    (tmp_path / 'test_generated.py').write_text(
        ''.join(f'def test_{i}():\n    pass\n\n\n' for i in range(how_many)),
        encoding='utf-8')
    return tmp_path


def test_collected_counts_a_real_directory(tmp_path):
    assert cf.collected(_suite(tmp_path, 7), []) == (7, None)


def test_collected_reads_an_empty_directory_as_zero_not_an_error(tmp_path):
    """pytest exits 5 here. That is a real answer, not a broken check."""
    assert cf.collected(tmp_path, []) == (0, None)


def test_collected_reports_a_broken_suite_as_an_error(tmp_path):
    """A file that cannot be imported makes pytest exit 2. The count it
    prints is of whatever else collected, which is not the number the floor
    should be compared against."""
    (tmp_path / 'test_broken.py').write_text('import nosuchmodule\n', encoding='utf-8')
    found, error = cf.collected(tmp_path, [])
    assert found == 0
    assert error is not None and 'exited 2' in error
