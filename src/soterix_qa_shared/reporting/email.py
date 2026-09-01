"""Email a test run report through Microsoft Graph.

A QA repo mails its HTML report to a distribution list after a run. The
client authenticates as an Azure app (client-credentials) and posts the
report as the mail body plus an attachment.

Two deliberate choices carried from the original:

  * No recipient default. The address comes from the caller or from
    REPORT_RECIPIENT, and a missing one is an error, so a report is never
    mailed to someone unexpected.
  * msal is imported lazily. It is a heavy dependency wanted only by a repo
    that emails reports, so it rides the `email` extra
    (`pip install soterix-qa-shared[email]`) and a caller without it gets one
    sentence naming the fix, not an ImportError several frames deep.

The package reads credentials from the environment and never loads a
`.env`: the consumer's process is already configured. Required vars:
AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, EMAIL_FROM.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import time
from datetime import datetime
from pathlib import Path

import requests

GRAPH = 'https://graph.microsoft.com'
ENV_VARS = ('AZURE_TENANT_ID', 'AZURE_CLIENT_ID', 'AZURE_CLIENT_SECRET', 'EMAIL_FROM')


class GraphEmailClient:
    """Sends mail as an Azure app through the Graph sendMail endpoint."""

    def __init__(self) -> None:
        self.tenant_id = os.environ.get('AZURE_TENANT_ID')
        self.client_id = os.environ.get('AZURE_CLIENT_ID')
        self.client_secret = os.environ.get('AZURE_CLIENT_SECRET')
        self.email_from = os.environ.get('EMAIL_FROM')
        missing = [v for v in ENV_VARS if not os.environ.get(v)]
        if missing:
            raise ValueError(
                f'Missing Azure OAuth environment variables: {", ".join(missing)}')

        try:
            from msal import ConfidentialClientApplication
        except ImportError as exc:
            raise RuntimeError(
                'msal is not installed, so mail cannot be sent. Run: '
                'pip install "soterix-qa-shared[email]"') from exc

        authority = f'https://login.microsoftonline.com/{self.tenant_id}'
        self.app = ConfidentialClientApplication(
            self.client_id, client_credential=self.client_secret,
            authority=authority)
        self._token: str | None = None
        self._token_expiry: float = 0.0

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        resp = self.app.acquire_token_for_client(
            scopes=[f'{GRAPH}/.default'])
        if not resp or 'access_token' not in resp:
            raise RuntimeError(f'Failed to acquire access token: {resp}')
        self._token = resp['access_token']
        self._token_expiry = time.time() + int(resp.get('expires_in', 3600))
        return self._token

    def _build_attachments(self, attachments: list[str]) -> list[dict]:
        files = []
        for path in attachments:
            if not os.path.exists(path):
                continue
            data = Path(path).read_bytes()
            ctype = mimetypes.guess_type(path)[0] or 'application/octet-stream'
            files.append({
                '@odata.type': '#microsoft.graph.fileAttachment',
                'name': os.path.basename(path),
                'contentType': ctype,
                'contentBytes': base64.b64encode(data).decode('ascii'),
            })
        return files

    def send_email(self, to: list[str] | str, subject: str, body_html: str,
                   attachments: list[str] | None = None) -> int:
        if isinstance(to, str):
            to = [to]
        headers = {'Authorization': f'Bearer {self._get_token()}',
                   'Content-Type': 'application/json'}
        message: dict = {
            'subject': subject,
            'body': {'contentType': 'HTML', 'content': body_html},
            'toRecipients': [{'emailAddress': {'address': a}} for a in to],
        }
        files = self._build_attachments(attachments or [])
        if files:
            message['attachments'] = files
        payload = {'message': message, 'saveToSentItems': True}
        url = f'{GRAPH}/v1.0/users/{self.email_from}/sendMail'
        r = requests.post(url, headers=headers, data=json.dumps(payload),
                          timeout=30)
        if r.status_code not in (200, 202):
            raise RuntimeError(f'Graph sendMail failed: {r.status_code} {r.text}')
        return r.status_code


_client: GraphEmailClient | None = None


def get_client() -> GraphEmailClient:
    global _client
    if _client is None:
        _client = GraphEmailClient()
    return _client


def reset_client() -> None:
    """Drop the cached client. For tests, and for a credential rotation."""
    global _client
    _client = None


def send_email(to: list[str] | str, subject: str, body_html: str,
               attachments: list[str] | None = None) -> int:
    return get_client().send_email(to, subject, body_html, attachments)


def send_report(report_path: Path, recipient: str | None = None) -> bool:
    """Mail an HTML report. `report_path` is absolute: the consumer resolves
    it against its own repo root, since the package holds no such path."""
    recipient = recipient or os.environ.get('REPORT_RECIPIENT')
    if not recipient:
        print('[ERROR] No recipient. Pass one, or set REPORT_RECIPIENT.')
        return False
    report_path = Path(report_path)
    if not report_path.exists():
        print(f'[ERROR] Report file not found: {report_path}')
        return False

    html = report_path.read_text(encoding='utf-8', errors='replace')
    title = 'Test Run Report'
    if '<title>' in html:
        title = html.split('<title>', 1)[1].split('</title>', 1)[0].strip() or title
    timestamp = datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S')
    body = (f'\n    <h2>{title}</h2>\n'
            f'    <p><strong>Generated:</strong> {timestamp}</p>\n'
            f'    <hr>\n    {html}\n    ')
    try:
        status = get_client().send_email(
            to=recipient, subject=f'Test Report: {title}',
            body_html=body, attachments=[str(report_path)])
        print(f'[SUCCESS] Email sent to {recipient} (status {status})')
        return True
    except Exception as e:  # noqa: BLE001 - a mailer reports any failure as False, never raises into a run
        print(f'[ERROR] Failed to send email: {e}')
        return False
