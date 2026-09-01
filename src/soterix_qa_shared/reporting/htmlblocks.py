"""Inline evidence blocks for a pytest-html report.

Helpers that render a request's response, or any labeled text, as a
collapsible block INSIDE the report row. Written for the API repo's
suites and shared here because nothing in them is API-specific: any repo
that generates a pytest-html report can attach evidence this way.

Why `html` extras and not `pytest_html.extras.text()` / `.json()`: with
--self-contained-html those render as a link to a data: URI, and modern
Chrome and Edge silently block top-level navigation to data: URLs from
file:// pages. The link appears and clicking it does nothing. The `html`
format is inserted inline by pytest-html's own frontend, so it works
regardless of browser data: URL policy. That finding is the reason this
module exists; keep it.

pytest-html is imported lazily so the base install stays report-free. A
consumer that calls `inline_block` without pytest-html installed gets one
sentence naming the fix, not an ImportError three frames deep.
"""
from __future__ import annotations

import html
import json
from typing import Any

MAX_BODY_CHARS = 5000


def _extras():
    try:
        # The submodule import, deliberately. A bare `import pytest_html`
        # only exposes `.extras` once pytest has loaded the plugin, so it
        # works inside a test session and fails in any other process.
        import pytest_html.extras
    except ImportError as exc:
        raise RuntimeError(
            'pytest-html is not installed, so an inline report block cannot '
            'be built. Run: pip install pytest-html') from exc
    return pytest_html.extras


def inline_block(label: str, content: str, open_by_default: bool = False) -> Any:
    """A collapsible <details> block for pytest-html's `html` extra format."""
    escaped = html.escape(content)
    open_attr = ' open' if open_by_default else ''
    return _extras().html(
        f"<details{open_attr} style='margin:4px 0;'>"
        f"<summary style='cursor:pointer;font-weight:600;'>{html.escape(label)}</summary>"
        f"<pre style='white-space:pre-wrap;word-break:break-word;"
        f"background:#f5f5f5;padding:8px;border-radius:4px;font-size:12px;'>{escaped}</pre>"
        f"</details>"
    )


def format_response_for_report(url: str, resp: Any) -> str:
    """A readable block for the report: URL, status, headers of interest,
    and the body - pretty-printed JSON where possible, truncated so a huge
    or binary body does not bloat the HTML file."""
    content_type = resp.headers.get('Content-Type', '')

    lines = [
        f'URL: {url}',
        f'Status: {resp.status_code}',
        f"Content-Type: {content_type or '(none)'}",
        f"Content-Length: {resp.headers.get('Content-Length', len(resp.content))}",
        '',
    ]

    if 'json' in content_type:
        try:
            body = json.dumps(resp.json(), indent=2, ensure_ascii=False)
        except ValueError:
            body = resp.text
    elif 'text' in content_type or content_type == '':
        body = resp.text
    else:
        body = f'[binary content, {len(resp.content)} bytes - not displayed]'

    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + f'\n... [truncated, {len(body)} total chars]'

    lines.append(body)
    return '\n'.join(lines)


def truncate_large_strings(obj: Any, max_len: int = 200) -> Any:
    """Recursively truncate long string values (base64 images, blobs) so a
    dumped response cannot balloon the report."""
    if isinstance(obj, dict):
        return {k: truncate_large_strings(v, max_len) for k, v in obj.items()}
    if isinstance(obj, list):
        return [truncate_large_strings(v, max_len) for v in obj]
    if isinstance(obj, str) and len(obj) > max_len:
        return obj[:max_len] + f'... [truncated, {len(obj)} total chars]'
    return obj
