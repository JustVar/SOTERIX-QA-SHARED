"""QA Touch pipeline: API client, status codes, mapping helpers.

The reporter plugin is deliberately NOT imported here. It needs pytest,
and pytest is an extra rather than a base dependency so a script-only
consumer (a script that only reads QA Touch) does not drag a test runner in. Import it
as `soterix_qa_shared.qatouch.reporter`.
"""
from .client import (
    LEGACY_CASE_FIELDS,
    PYTEST_TO_QATOUCH,
    STATUS_BLOCKED,
    STATUS_FAILED,
    STATUS_HOLD,
    STATUS_IN_PROGRESS,
    STATUS_NAMES,
    STATUS_NOT_APPLICABLE,
    STATUS_PASSED,
    STATUS_RETEST,
    STATUS_UNTESTED,
    QATouchClient,
    QATouchError,
    add_legacy_case_aliases,
    case_code_to_ref,
    normalize_status_name,
    status_code_for,
)
from .mapping import base_keyed, function_base

__all__ = [
    "LEGACY_CASE_FIELDS",
    "PYTEST_TO_QATOUCH",
    "STATUS_BLOCKED",
    "STATUS_FAILED",
    "STATUS_HOLD",
    "STATUS_IN_PROGRESS",
    "STATUS_NAMES",
    "STATUS_NOT_APPLICABLE",
    "STATUS_PASSED",
    "STATUS_RETEST",
    "STATUS_UNTESTED",
    "QATouchClient",
    "QATouchError",
    "add_legacy_case_aliases",
    "base_keyed",
    "case_code_to_ref",
    "function_base",
    "normalize_status_name",
    "status_code_for",
]
