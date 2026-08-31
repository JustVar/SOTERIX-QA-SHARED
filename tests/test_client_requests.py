"""Offline tests for the client's request shaping and input guards.

No network: every test swaps the client's session for a recorder that
captures the call and answers a canned response. These pin the three
behaviours the reporter and the upload tools depend on and that
test_client_helpers.py does not reach - the case-ref conversion at its
edges, the update whitelist, and the two request shapes of the
by-code result endpoint.
"""
import json

import pytest

from soterix_qa_shared.qatouch import QATouchClient, QATouchError, case_code_to_ref


class _Response:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = {"success": True} if payload is None else payload
        self.text = text

    def json(self):
        return self._payload


class _Recorder:
    """Stands in for requests.Session and records every call."""

    def __init__(self, status_code=200, payload=None):
        self.calls = []
        self._status_code = status_code
        self._payload = payload
        self.headers = {}

    def _record(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, "kwargs": kwargs})
        return _Response(self._status_code, self._payload)

    def get(self, url, **kwargs):
        return self._record("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._record("POST", url, **kwargs)

    def patch(self, url, **kwargs):
        return self._record("PATCH", url, **kwargs)

    @property
    def last(self):
        return self.calls[-1]


@pytest.fixture
def client():
    """A client with no env dependency and no live session."""
    c = QATouchClient(api_key="test-key", domain="test-domain")
    c.session = _Recorder()
    return c


# --------------------------------------------------------------------- #
#  1. case_code_to_ref at its edges
# --------------------------------------------------------------------- #

def test_case_code_to_ref_keeps_only_digits_across_separators():
    """Digits are concatenated, so separators and letters vanish."""
    assert case_code_to_ref("TR-01-99") == "199"
    assert case_code_to_ref("TR01A5") == "15"
    assert case_code_to_ref(262) == "262"


@pytest.mark.parametrize("empty", ["", "   ", "abc", "TR", "TR0000", None])
def test_case_code_to_ref_degrades_to_zero_not_an_error(empty):
    """A code with no usable digits becomes "0" - it does NOT raise.

    This is the documented landmine, pinned deliberately. "0" is a
    well-formed ref, so update_test_run_results (which answers success for
    refs that match nothing) accepts it and silently changes no status.
    Callers must reject a falsy case code themselves; the helper will not
    do it for them. If this behaviour is ever tightened to raise, this
    test is the one that must change, on purpose.
    """
    assert case_code_to_ref(empty) == "0"


# --------------------------------------------------------------------- #
#  2. The CASE_UPDATE_FIELDS whitelist
# --------------------------------------------------------------------- #

def test_update_test_case_rejects_section_key_the_api_silently_ignores():
    """sectionKey is the documented trap: accepted by the API, never applied.

    The whitelist must keep rejecting it, or a caller gets {"success": true}
    for a module move that did not happen.
    """
    c = QATouchClient(api_key="k", domain="d")
    c.session = _Recorder()
    assert "sectionKey" not in QATouchClient.CASE_UPDATE_FIELDS
    with pytest.raises(QATouchError, match="sectionKey"):
        c.update_test_case("JlW1", "B869bm", sectionKey="X4x9a")
    assert c.session.calls == [], "a rejected field must not reach the network"


def test_update_test_case_rejects_tags_and_says_why(client):
    with pytest.raises(QATouchError, match="Tags cannot be changed"):
        client.update_test_case("JlW1", "B869bm", tags="smoke")
    assert client.session.calls == []


def test_update_test_case_refuses_an_empty_update(client):
    with pytest.raises(QATouchError, match="nothing to update"):
        client.update_test_case("JlW1", "B869bm")
    assert client.session.calls == []


def test_update_test_case_maps_every_whitelisted_name_to_its_parameter(client):
    """Each accepted name must arrive under the query parameter the API wants."""
    for name, param in QATouchClient.CASE_UPDATE_FIELDS.items():
        client.session = _Recorder()
        client.update_test_case("JlW1", "B869bm", **{name: "value"})
        params = client.session.last["kwargs"]["params"]
        assert params[param] == "value", f"{name} should be sent as {param}"
        assert params["projectKey"] == "JlW1"
        assert params["caseKey"] == "B869bm"
        # "title" is an alias, not a second field.
        assert "title" not in params


def test_update_test_case_posts_to_the_update_endpoint(client):
    client.update_test_case("JlW1", "B869bm", title="New title")
    call = client.session.last
    assert call["method"] == "POST"
    assert call["url"].endswith("/api/v1/testcase/update")
    assert call["kwargs"]["params"]["caseTitle"] == "New title"


# --------------------------------------------------------------------- #
#  3. Request shaping in update_test_run_results_by_code
# --------------------------------------------------------------------- #

RESULTS = [{"case": "TR0263", "status": 1}, {"case": "TR0264", "status": 5}]


def test_by_code_plain_upload_sends_no_files_and_no_header_override(client):
    """No attachments: the request must stay what it was before attachments.

    The client's own comment claims this path is byte-identical to the
    pre-attachment version. That claim is only true while neither `files`
    nor `headers` is passed, so pin both.
    """
    client.update_test_run_results_by_code("JlW1", "vaQly", RESULTS)
    kwargs = client.session.last["kwargs"]
    assert "files" not in kwargs
    assert "headers" not in kwargs
    assert set(kwargs) == {"params", "timeout"}


def test_by_code_wraps_results_in_an_index_keyed_object_not_a_list(client):
    """The endpoint wants {"0": {...}, "1": {...}}, not a JSON array."""
    client.update_test_run_results_by_code("JlW1", "vaQly", RESULTS)
    params = client.session.last["kwargs"]["params"]
    assert params["project"] == "JlW1"
    assert params["test_run"] == "vaQly"
    payload = json.loads(params["result"])
    assert payload == {
        "0": {"case": "TR0263", "status": 1},
        "1": {"case": "TR0264", "status": 5},
    }
    # The full code travels, prefix and zeros intact - not the stripped ref.
    assert payload["0"]["case"] == "TR0263"


def test_by_code_omits_comments_unless_given(client):
    client.update_test_run_results_by_code("JlW1", "vaQly", RESULTS)
    assert "comments" not in client.session.last["kwargs"]["params"]
    client.session = _Recorder()
    client.update_test_run_results_by_code("JlW1", "vaQly", RESULTS, comments="nightly")
    assert client.session.last["kwargs"]["params"]["comments"] == "nightly"


def test_by_code_attachments_switch_to_multipart_and_drop_the_json_header(client):
    """Attachments go as repeated file[] parts with the session header removed.

    The session pins Content-Type: application/json. requests must be left
    to write its own multipart boundary instead, which it only does when the
    pinned value is removed by passing None.
    """
    attachments = [
        ("fail.png", b"\x89PNG", "image/png"),
        ("run.log", b"trace", "text/plain"),
    ]
    client.update_test_run_results_by_code(
        "JlW1", "vaQly", RESULTS, attachments=attachments
    )
    kwargs = client.session.last["kwargs"]
    assert kwargs["headers"] == {"Content-Type": None}
    assert kwargs["files"] == [
        ("file[]", ("fail.png", b"\x89PNG", "image/png")),
        ("file[]", ("run.log", b"trace", "text/plain")),
    ]
    # The params still carry the results; attachments do not replace them.
    assert json.loads(kwargs["params"]["result"])["0"]["case"] == "TR0263"


def test_by_code_raises_on_a_non_200(client):
    client.session = _Recorder(status_code=500)
    with pytest.raises(QATouchError, match="failed: 500"):
        client.update_test_run_results_by_code("JlW1", "vaQly", RESULTS)


def test_by_code_raises_when_the_body_says_not_successful(client):
    client.session = _Recorder(payload={"success": False, "error_msg": "bad run key"})
    with pytest.raises(QATouchError, match="bad run key"):
        client.update_test_run_results_by_code("JlW1", "vaQly", RESULTS)


# ---------------------------------------------------------------------------
# verify_results_landed is the one guard the whole upload path trusts
# ---------------------------------------------------------------------------

def _verifier(run_status='Untested'):
    from unittest.mock import MagicMock
    c = QATouchClient(api_key='k', domain='d')
    c.get_all_test_run_results = MagicMock(
        return_value=[{'code': 'TR0001', 'status': run_status}])
    return c


def test_verify_reports_a_real_mismatch():
    r = _verifier().verify_results_landed('P', 'RUN', [{'case': 'TR0001', 'status': 5}])
    assert r['landed'] == [] and len(r['mismatched']) == 1


def test_verify_reports_a_real_landing():
    r = _verifier('Failed').verify_results_landed('P', 'RUN', [{'case': 'TR0001', 'status': 5}])
    assert r['landed'] == ['TR0001'] and r['mismatched'] == []


def _run_of(*codes, status='Untested'):
    """A client whose run holds exactly these case codes."""
    from unittest.mock import MagicMock
    c = QATouchClient(api_key='k', domain='d')
    c.get_all_test_run_results = MagicMock(
        return_value=[{'code': code, 'status': status} for code in codes])
    return c


def test_verify_names_the_cases_the_upload_said_nothing_about():
    """The real PgGMv run. Five cases, four set, every check green, and the
    run still held an Untested case because the selected tests did not
    cover it. Nothing in the output said so."""
    client = _run_of('TR3681', 'TR3682', 'TR3683', 'TR3684', 'TR3685',
                     status='Passed')
    sent = [{'case': c, 'status': 1}
            for c in ('TR3681', 'TR3682', 'TR3683', 'TR3684')]
    r = client.verify_results_landed('P', 'PgGMv', sent)
    assert r['landed'] == ['TR3681', 'TR3682', 'TR3683', 'TR3684']
    assert r['mismatched'] == [] and r['missing'] == []
    assert r['in_run'] == 5
    assert r['uncovered'] == ['TR3685']


def test_verify_reports_no_shortfall_when_the_run_is_covered():
    client = _run_of('TR1', 'TR2', status='Passed')
    r = client.verify_results_landed('P', 'RUN', [{'case': 'TR1', 'status': 1},
                                                 {'case': 'TR2', 'status': 1}])
    assert r['in_run'] == 2
    assert r['uncovered'] == []


def test_a_code_absent_from_the_run_is_not_counted_as_uncovered():
    """'missing' and 'uncovered' are opposite errors: a code sent that the
    run does not hold, and a case the run holds that was not sent. Mixing
    them would report a typo as a coverage gap."""
    r = _run_of('TR1').verify_results_landed(
        'P', 'RUN', [{'case': 'TR9999', 'status': 5}])
    assert r['missing'] == ['TR9999']
    assert r['uncovered'] == ['TR1']


def test_verify_reports_a_code_absent_from_the_run():
    r = _verifier().verify_results_landed('P', 'RUN', [{'case': 'TR9999', 'status': 5}])
    assert r['missing'] == ['TR9999']


@pytest.mark.parametrize('status', [0, 99, -1, 'five', '', None])
def test_a_status_this_client_cannot_name_is_never_landed(status):
    """Measured before this: status 0, an unknown code and a missing key all
    reported LANDED while the run still read Untested, and None raised
    TypeError. A status the client cannot name is a status it cannot check,
    so it is a mismatch - the retry CLI replays whatever is on disk."""
    r = _verifier().verify_results_landed('P', 'RUN', [{'case': 'TR0001', 'status': status}])
    assert r['landed'] == []
    assert len(r['mismatched']) == 1
    assert 'cannot be checked' in r['mismatched'][0]


def test_a_missing_status_key_is_never_landed():
    r = _verifier().verify_results_landed('P', 'RUN', [{'case': 'TR0001'}])
    assert r['landed'] == [] and len(r['mismatched']) == 1
