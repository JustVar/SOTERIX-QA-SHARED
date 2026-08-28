"""Offline unit tests for the QA Touch client helpers.

These pin the pure functions the reporter and tools depend on. No network.
"""
from soterix_qa_shared.qatouch import (
    PYTEST_TO_QATOUCH,
    STATUS_NAMES,
    QATouchClient,
    add_legacy_case_aliases,
    case_code_to_ref,
    normalize_status_name,
    status_code_for,
)


def test_case_code_to_ref_strips_prefix_and_zeros():
    assert case_code_to_ref("TR0262") == "262"
    assert case_code_to_ref("NE-3681") == "3681"
    assert case_code_to_ref("0") == "0"


def test_normalize_status_folds_spellings():
    assert normalize_status_name("Not-Applicable") == "not applicable"
    assert normalize_status_name("Not Applicable") == "not applicable"
    assert normalize_status_name("In Progress") == "in progress"


def test_status_code_for_both_spellings():
    assert status_code_for("In-Progress") == 7
    assert status_code_for("Not Applicable") == 6
    assert status_code_for("nonsense") is None


def test_pytest_status_map():
    assert PYTEST_TO_QATOUCH == {"passed": 1, "failed": 5, "skipped": 3}


def test_status_names_has_all_eight():
    assert set(STATUS_NAMES) == set(range(1, 9))


def test_legacy_aliases_add_old_names():
    out = add_legacy_case_aliases({"module_name": "Views", "Tags": ["a", "b"]})
    assert out["module name"] == "Views"
    assert out["NewTags"] == "a, b"


def test_client_has_the_unified_surface():
    # The methods unification requires from both repos' clients.
    for name in ("create_test_case", "create_test_case_with_steps",
                 "update_test_run_results_by_code", "verify_results_landed",
                 "update_test_case", "find_case_by_title"):
        assert hasattr(QATouchClient, name), name
