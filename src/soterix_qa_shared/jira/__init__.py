"""Filing a Jira bug from a failed test.

Two pieces, and only two. Deciding *whether* a failure deserves a bug is
each repo's own business, so the flaky history, the confidence gates and the
approval queue stay where they are.

  * `client`  creates the issue, or describes the one it would create
  * `fields`  maps a test's data onto a Jira project's custom field ids

Both need something from the consumer first:

    from soterix_qa_shared.jira import JiraClient, fields

    fields.configure(REPO_ROOT / 'config' / 'jira_field_mapping.json')
    result = JiraClient().create_issue(summary, description,
                                       qa_touch_data={...})
    print(result['key'])

Filing is the one action here another team sees, so it is gated twice:
`BUG_CREATION_ENABLED` must be true and the `jira` package must be
installed. Otherwise `create_issue` returns a dry-run dict describing the
issue, including its mapped fields, and reaches nothing.

`pip install soterix-qa-shared[jira]` adds the `jira` package. The base
install can import this subpackage and run the dry-run path without it.
"""
from .client import (
    ENABLE_VAR,
    TEST_LABEL,
    TEST_MARKER,
    JiraClient,
    creation_enabled,
)
from .fields import (
    JIRA_TO_QA_TOUCH_STATUS,
    NC_ISSUE_TYPES,
    PRIORITY_TO_SEVERITY,
    QA_TOUCH_TO_JIRA_STATUS,
    QATouchToJiraMapper,
    get_field_allowed_values,
    get_required_field_ids,
    load_discovered_fields,
    mapper,
)

__all__ = [
    "ENABLE_VAR",
    "JIRA_TO_QA_TOUCH_STATUS",
    "NC_ISSUE_TYPES",
    "PRIORITY_TO_SEVERITY",
    "QA_TOUCH_TO_JIRA_STATUS",
    "TEST_LABEL",
    "TEST_MARKER",
    "JiraClient",
    "QATouchToJiraMapper",
    "creation_enabled",
    "get_field_allowed_values",
    "get_required_field_ids",
    "load_discovered_fields",
    "mapper",
]
