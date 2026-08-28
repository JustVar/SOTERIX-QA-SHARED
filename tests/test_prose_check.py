"""The prose checker.

It has one job it must not fail at: staying quiet about code. A rule that
fires on a shell command is a rule people turn off, and then the real
findings go with it.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.prose_check import ERRORS, check, main, strip_code

DASH = chr(8212)
LQUOTE = chr(8220)


def _md(tmp_path, text):
    p = tmp_path / 'doc.md'
    p.write_text(text, encoding='utf-8')
    return p


def test_a_dash_in_prose_is_reported(tmp_path):
    findings = check(_md(tmp_path, f'A sentence {DASH} and more.'), ERRORS)
    assert len(findings) == 1
    assert 'dash' in findings[0]


def test_a_dash_inside_a_fence_is_not_reported(tmp_path):
    findings = check(_md(tmp_path, f'Text.\n\n```\ncmd {DASH} arg\n```\n'), ERRORS)
    assert findings == []


def test_a_dash_inside_an_inline_span_is_not_reported(tmp_path):
    findings = check(_md(tmp_path, f'Run `a {DASH} b` now.'), ERRORS)
    assert findings == []


def test_a_dash_in_a_link_target_is_not_reported(tmp_path):
    """A file really can be named with a dash. That is a path, not prose."""
    findings = check(_md(tmp_path, f'See [the doc](docs/a{DASH}b.md).'), ERRORS)
    assert findings == []


def test_stripping_code_keeps_line_numbers(tmp_path):
    """A finding on line 9 must say line 9, or nobody trusts the next one."""
    text = 'one\n\n```\ntwo\nthree\n```\n\nfour\n' + f'five {DASH} six\n'
    assert len(strip_code(text).splitlines()) == len(text.splitlines())
    findings = check(_md(tmp_path, text), ERRORS)
    assert ':9:' in findings[0]


def test_hype_and_filler_are_reported(tmp_path):
    findings = check(_md(tmp_path, 'A robust design, very simply put.'), ERRORS)
    kinds = {f.split(': ')[1] for f in findings}
    assert kinds == {'hype', 'filler'}


def test_a_smart_quote_is_reported(tmp_path):
    findings = check(_md(tmp_path, f'He said {LQUOTE}hello.'), ERRORS)
    assert 'smart-quote' in findings[0]


def test_a_markdown_image_is_not_an_exclamation(tmp_path):
    """![alt](src) starts with a bang and is not a shout."""
    assert check(_md(tmp_path, '![alt](img.png)'), ERRORS) == []


def test_a_file_outside_the_repo_does_not_crash(tmp_path):
    """Found by probing the checker with a scratch file: relative_to raised."""
    assert main([str(_md(tmp_path, 'Clean text.'))]) == 0


def test_the_exit_code_reports_findings(tmp_path):
    assert main([str(_md(tmp_path, 'Clean text.'))]) == 0
    assert main([str(_md(tmp_path, f'Dirty {DASH} text.'))]) == 1


def test_this_repos_own_docs_pass():
    """The rule has to hold here first, or it holds nowhere."""
    assert main([]) == 0
