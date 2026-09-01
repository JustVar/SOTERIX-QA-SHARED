"""Inline report blocks.

Offline. A fake response object stands in for requests' - the module only
reads .headers/.status_code/.content/.text/.json(), and pinning that small
surface here is the point: it is the contract a consumer's client must meet.
"""
from __future__ import annotations

import pytest

from soterix_qa_shared.reporting import htmlblocks as hb


class _Resp:
    def __init__(self, status=200, ctype='application/json', text='{}',
                 content=b'{}', payload=None):
        self.status_code = status
        self.headers = {'Content-Type': ctype} if ctype is not None else {}
        self.text = text
        self.content = content
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError('not json')
        return self._payload


# ---------------------------------------------------------------------------
# inline_block
# ---------------------------------------------------------------------------

def test_inline_block_escapes_content_and_label():
    """Report evidence is untrusted text. A response body containing markup
    must render as text, not execute in the report."""
    got = hb.inline_block('<b>label</b>', '<script>alert(1)</script>')
    assert '<script>' not in got['content']
    assert '&lt;script&gt;' in got['content']
    assert '&lt;b&gt;label&lt;/b&gt;' in got['content']


def test_inline_block_is_closed_by_default_and_openable():
    """The block starts collapsed unless the caller opens it."""
    closed = hb.inline_block('l', 'c')['content']
    assert '<details open' not in closed
    assert '<details open' in hb.inline_block('l', 'c', open_by_default=True)['content']


def test_inline_block_uses_the_html_extra_not_a_data_uri():
    """The reason this module exists: text()/json() extras become data: URI
    links that Chrome blocks from file:// pages. The html extra is inline."""
    got = hb.inline_block('l', 'c')
    assert got['format_type'] == 'html'


def test_a_missing_pytest_html_names_the_fix(monkeypatch):
    """Without pytest-html, the error is one sentence naming the fix.

    Two seams have to be closed for the simulation to hold: the import
    hook must refuse the SUBMODULE name (`pytest_html.extras`, which is
    what the module imports), and sys.modules must be cleared of both
    entries, because a cached module satisfies an import statement without
    __import__ ever being consulted for a fresh load.
    """
    import builtins
    import sys
    real = builtins.__import__

    def refuse(name, *a, **k):
        if name.startswith('pytest_html'):
            raise ImportError(name)
        return real(name, *a, **k)
    monkeypatch.delitem(sys.modules, 'pytest_html', raising=False)
    monkeypatch.delitem(sys.modules, 'pytest_html.extras', raising=False)
    monkeypatch.setattr(builtins, '__import__', refuse)
    with pytest.raises(RuntimeError, match='pip install pytest-html'):
        hb.inline_block('l', 'c')


# ---------------------------------------------------------------------------
# format_response_for_report
# ---------------------------------------------------------------------------

def test_json_bodies_are_pretty_printed():
    resp = _Resp(payload={'a': [1, 2]}, text='{"a":[1,2]}')
    got = hb.format_response_for_report('https://x/y', resp)
    assert 'URL: https://x/y' in got
    assert 'Status: 200' in got
    assert '"a": [' in got                       # indented, not the raw text


def test_a_json_content_type_with_a_broken_body_falls_back_to_text():
    resp = _Resp(payload=None, text='not json at all')
    assert 'not json at all' in hb.format_response_for_report('u', resp)


def test_text_and_missing_content_types_use_the_text():
    assert 'plain' in hb.format_response_for_report('u', _Resp(ctype='text/plain', text='plain'))
    assert 'bare' in hb.format_response_for_report('u', _Resp(ctype='', text='bare'))


def test_binary_bodies_are_described_not_dumped():
    resp = _Resp(ctype='image/png', content=b'\x89PNG' * 100)
    got = hb.format_response_for_report('u', resp)
    assert 'binary content, 400 bytes' in got
    assert 'PNG' not in got.split('binary')[1][:40]


def test_huge_bodies_are_truncated_with_the_total_named():
    resp = _Resp(ctype='text/plain', text='x' * 9000)
    got = hb.format_response_for_report('u', resp)
    assert len(got) < 6000
    assert 'truncated, 9000 total chars' in got


# ---------------------------------------------------------------------------
# truncate_large_strings
# ---------------------------------------------------------------------------

def test_truncation_recurses_through_dicts_and_lists():
    data = {'pic': 'A' * 500, 'items': [{'blob': 'B' * 300}, 'short']}
    got = hb.truncate_large_strings(data)
    assert got['pic'].startswith('A' * 200)
    assert '500 total chars' in got['pic']
    assert '300 total chars' in got['items'][0]['blob']
    assert got['items'][1] == 'short'


def test_short_values_and_non_strings_pass_through_unchanged():
    data = {'n': 42, 's': 'ok', 'f': 1.5, 'none': None}
    assert hb.truncate_large_strings(data) == data
