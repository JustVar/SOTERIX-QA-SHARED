"""The report mailer.

Offline: requests and msal are stubbed. The Graph client is mostly about
NOT sending - no recipient, a missing report, a missing credential - because
a wrong send reaches a distribution list.
"""
from __future__ import annotations

import sys
import types

import pytest

from soterix_qa_shared.reporting import email as em


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    em.reset_client()
    for v in em.ENV_VARS:
        monkeypatch.setenv(v, f'test-{v.lower()}')
    monkeypatch.delenv('REPORT_RECIPIENT', raising=False)
    yield
    em.reset_client()


def _fake_msal(monkeypatch, token='tok'):
    """A fake msal module so no real auth happens."""
    app = types.SimpleNamespace(
        acquire_token_for_client=lambda scopes: {'access_token': token,
                                                 'expires_in': 3600})
    mod = types.ModuleType('msal')
    mod.ConfidentialClientApplication = lambda *a, **k: app
    monkeypatch.setitem(sys.modules, 'msal', mod)


# ---------------------------------------------------------------------------
# Refusing to send
# ---------------------------------------------------------------------------

def test_no_recipient_is_an_error_not_a_send(tmp_path, capsys):
    report = tmp_path / 'r.html'
    report.write_text('<title>X</title>', encoding='utf-8')
    assert em.send_report(report) is False
    assert 'No recipient' in capsys.readouterr().out


def test_a_missing_report_is_an_error(tmp_path, capsys):
    assert em.send_report(tmp_path / 'nope.html', 'a@b.com') is False
    assert 'not found' in capsys.readouterr().out


def test_a_missing_credential_names_the_variable(monkeypatch):
    monkeypatch.delenv('AZURE_TENANT_ID', raising=False)
    with pytest.raises(ValueError, match='AZURE_TENANT_ID'):
        em.GraphEmailClient()


def test_msal_absent_names_the_extra(monkeypatch):
    """The base install has no msal. The error is one sentence naming the
    extra, not an ImportError from inside the client."""
    import builtins
    real = builtins.__import__

    def refuse(name, *a, **k):
        if name == 'msal':
            raise ImportError(name)
        return real(name, *a, **k)
    monkeypatch.delitem(sys.modules, 'msal', raising=False)
    monkeypatch.setattr(builtins, '__import__', refuse)
    with pytest.raises(RuntimeError, match=r'soterix-qa-shared\[email\]'):
        em.GraphEmailClient()


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

def test_send_report_posts_the_report_and_reports_success(tmp_path, monkeypatch, capsys):
    _fake_msal(monkeypatch)
    posted = {}

    def fake_post(url, headers, data, timeout):
        posted['url'] = url
        posted['data'] = data
        return types.SimpleNamespace(status_code=202, text='')
    monkeypatch.setattr(em.requests, 'post', fake_post)

    report = tmp_path / 'report.html'
    report.write_text('<title>Nightly UI</title><body>ok</body>', encoding='utf-8')
    assert em.send_report(report, 'qa@example.com') is True
    assert 'sendMail' in posted['url']
    assert 'Nightly UI' in posted['data']              # the title rode through
    assert 'SUCCESS' in capsys.readouterr().out


def test_the_recipient_falls_back_to_the_environment(tmp_path, monkeypatch):
    _fake_msal(monkeypatch)
    monkeypatch.setenv('REPORT_RECIPIENT', 'list@example.com')
    seen = {}
    monkeypatch.setattr(em.requests, 'post',
                        lambda url, headers, data, timeout:
                        seen.update(data=data) or types.SimpleNamespace(status_code=200, text=''))
    report = tmp_path / 'r.html'
    report.write_text('<title>t</title>', encoding='utf-8')
    assert em.send_report(report) is True
    assert 'list@example.com' in seen['data']


def test_a_graph_error_is_caught_and_reported_false(tmp_path, monkeypatch, capsys):
    _fake_msal(monkeypatch)
    monkeypatch.setattr(em.requests, 'post',
                        lambda url, headers, data, timeout:
                        types.SimpleNamespace(status_code=500, text='boom'))
    report = tmp_path / 'r.html'
    report.write_text('<title>t</title>', encoding='utf-8')
    assert em.send_report(report, 'a@b.com') is False
    assert 'Failed to send' in capsys.readouterr().out


def test_attachments_that_do_not_exist_are_skipped(tmp_path, monkeypatch):
    _fake_msal(monkeypatch)
    monkeypatch.setattr(em.requests, 'post',
                        lambda url, headers, data, timeout:
                        types.SimpleNamespace(status_code=202, text=''))
    real = tmp_path / 'real.html'
    real.write_text('hi', encoding='utf-8')
    client = em.GraphEmailClient()
    files = client._build_attachments([str(real), str(tmp_path / 'ghost.html')])
    assert len(files) == 1
    assert files[0]['name'] == 'real.html'
