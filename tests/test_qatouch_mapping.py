"""Pins for qatouch.mapping."""
from __future__ import annotations


def test_the_xdist_group_suffix_is_not_part_of_a_test_identity():
    """'@account_views' goes, a parameter that holds an '@' stays, and function_base sees through the two."""
    import pytest

    from soterix_qa_shared.qatouch import mapping

    cases = [
        ('tests/ui/a.py::T::test_x[chromium]@account_views', 'tests/ui/a.py::T::test_x[chromium]'),
        ('tests/ui/a.py::T::test_x[chromium]', 'tests/ui/a.py::T::test_x[chromium]'),
        ('tests/ui/a.py::test_mail[a@b.com]', 'tests/ui/a.py::test_mail[a@b.com]'),
        ('tests/ui/a.py::test_mail[a@b.com]@grp', 'tests/ui/a.py::test_mail[a@b.com]'),
        ('tests/ui/a.py::test_plain@grp', 'tests/ui/a.py::test_plain'),
    ]
    for given, want in cases:
        assert mapping.canonical_nodeid(given) == want, given
    assert mapping.function_base('tests/ui/a.py::T::test_x[chromium]@account_views') == 'tests/ui/a.py::T::test_x'
    assert pytest  # imported for parity with the other pins of this file

