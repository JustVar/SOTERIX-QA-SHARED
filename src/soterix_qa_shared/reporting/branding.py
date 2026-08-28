"""Company branding for the generated report pages.

Two PNGs under a consumer's `assets/branding/` carry the logo:
`logo-wide.png` for page headers, `logo-square.png` for the browser tab
icon. Replace the files to change the logo everywhere. Pages embed the bytes
at build time, so they stay self-contained and an older report keeps the
logo it was built with. A missing file leaves the page unbranded, which is
the correct outcome: a report is still readable without a logo.

The paths came from `Path('assets') / 'branding'`, relative to the working
directory. That worked only because every caller happened to run from the
repo root, and after packaging it would resolve against whatever directory
the consumer was invoked from. `configure(root)` now takes the root, the
same way the reporter does.
"""
from __future__ import annotations

import base64
from functools import lru_cache
from html import escape
from pathlib import Path

COMPANY = 'Soterix Systems'

BRAND_DIR: Path | None = None
WIDE_LOGO: Path | None = None
SQUARE_LOGO: Path | None = None


def configure(root: Path | str) -> None:
    """Point the logo paths at a consumer repo root.

    The cache is cleared here. It is keyed by path string, so a stale entry
    would not survive a changed root, but a test that writes a new PNG to
    the same path would read the old bytes. Clearing costs nothing and
    removes the question.
    """
    global BRAND_DIR, WIDE_LOGO, SQUARE_LOGO
    BRAND_DIR = Path(root) / 'assets' / 'branding'
    WIDE_LOGO = BRAND_DIR / 'logo-wide.png'
    SQUARE_LOGO = BRAND_DIR / 'logo-square.png'
    _data_uri.cache_clear()


def configure_reset() -> None:
    """Forget the configured root. For tests, and for a CLI that finishes."""
    global BRAND_DIR, WIDE_LOGO, SQUARE_LOGO
    BRAND_DIR = WIDE_LOGO = SQUARE_LOGO = None
    _data_uri.cache_clear()


@lru_cache(maxsize=4)
def _data_uri(path: str) -> str:
    """The file as a PNG data URI, or '' when it is not there."""
    if not path:
        return ''
    try:
        return ('data:image/png;base64,'
                + base64.b64encode(Path(path).read_bytes()).decode('ascii'))
    except OSError:
        return ''


def favicon_link() -> str:
    """The <link rel="icon"> tag for a page head, or '' without a square logo."""
    uri = _data_uri(str(SQUARE_LOGO) if SQUARE_LOGO else '')
    return f'<link rel="icon" type="image/png" href="{uri}">' if uri else ''


def header_logo(height_px: int = 44) -> str:
    """The <img> for a page header, or '' without a wide logo."""
    uri = _data_uri(str(WIDE_LOGO) if WIDE_LOGO else '')
    if not uri:
        return ''
    return (f'<img class="brand" src="{uri}" alt="{escape(COMPANY)}" '
            f'height="{height_px}" style="height:{height_px}px;width:auto">')
