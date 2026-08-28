"""Unit tests for soterix_qa_shared.reporting.branding.

The paths used to be relative to the working directory, which happened to
work because every caller ran from the repo root. After packaging that is
no longer true, so the root arrives through configure().
"""
from __future__ import annotations

import pytest

from soterix_qa_shared.reporting import branding

PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 16


@pytest.fixture(autouse=True)
def _clean():
    yield
    branding.configure_reset()


def _branded(root):
    (root / 'assets' / 'branding').mkdir(parents=True, exist_ok=True)
    (root / 'assets' / 'branding' / 'logo-square.png').write_bytes(PNG)
    (root / 'assets' / 'branding' / 'logo-wide.png').write_bytes(PNG)
    return root


def test_configure_derives_both_logo_paths(tmp_path):
    branding.configure(tmp_path)
    assert branding.BRAND_DIR == tmp_path / 'assets' / 'branding'
    assert branding.WIDE_LOGO == branding.BRAND_DIR / 'logo-wide.png'
    assert branding.SQUARE_LOGO == branding.BRAND_DIR / 'logo-square.png'


def test_favicon_and_header_embed_the_files(tmp_path):
    """Both tags embed the PNG bytes as a data URI when the files exist."""
    branding.configure(_branded(tmp_path))
    assert branding.favicon_link().startswith(
        '<link rel="icon" type="image/png" href="data:image/png;base64,')
    img = branding.header_logo(40)
    assert 'data:image/png;base64,' in img
    assert 'height:40px' in img
    assert 'Soterix Systems' in img


def test_missing_files_leave_the_page_unbranded(tmp_path):
    """A report is still readable without a logo, so a missing file is not
    an error."""
    branding.configure(tmp_path)
    assert branding.favicon_link() == ''
    assert branding.header_logo() == ''


def test_without_configure_nothing_is_branded_and_nothing_raises():
    """The package holds no path of its own. A consumer that forgets to
    configure gets an unbranded page, not a crash mid-report."""
    branding.configure_reset()
    assert branding.BRAND_DIR is None
    assert branding.favicon_link() == ''
    assert branding.header_logo() == ''


def test_configure_clears_the_cache_so_a_new_root_is_read(tmp_path):
    """The cache is keyed by path string. Two roots with the same layout
    would still be distinct keys, but a rebuilt file at the SAME path
    would serve stale bytes, and a report would carry the old logo."""
    root = _branded(tmp_path / 'one')
    branding.configure(root)
    first = branding.header_logo()
    (root / 'assets' / 'branding' / 'logo-wide.png').write_bytes(PNG + b'CHANGED')
    branding.configure(root)                       # same root, new bytes
    assert branding.header_logo() != first


def test_a_directory_where_a_logo_should_be_is_not_an_error(tmp_path):
    """OSError, not just FileNotFoundError: a stray directory named
    logo-wide.png reads as IsADirectoryError."""
    (tmp_path / 'assets' / 'branding' / 'logo-wide.png').mkdir(parents=True)
    branding.configure(tmp_path)
    assert branding.header_logo() == ''


def test_the_company_name_is_html_escaped(tmp_path, monkeypatch):
    """The alt text goes straight into a page."""
    monkeypatch.setattr(branding, 'COMPANY', 'A & B <script>')
    branding.configure(_branded(tmp_path))
    assert 'A &amp; B &lt;script&gt;' in branding.header_logo()
