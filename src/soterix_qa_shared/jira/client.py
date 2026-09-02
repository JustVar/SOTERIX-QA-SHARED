"""Creating a Jira issue from a failed test.

Filing a bug is the one action in this pipeline that another team sees, so
it is gated twice: `BUG_CREATION_ENABLED` must be true AND the `jira`
package must be installed. With either missing, `create_issue` returns a
dry-run dict describing what it would have filed, including the mapped
custom fields, so the whole path can be exercised with nothing reaching
Jira. `create_issue` always returns a dict with a `key`, and post-create
work (labels, comments) never depends on a story link being passed.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

from soterix_qa_shared.jira.fields import BUG_LIKE, staleness_warning
from soterix_qa_shared.jira.fields import mapper as default_mapper

log = logging.getLogger('soterix_qa_shared.jira')

try:                                    # pragma: no cover - import shape
    from jira import JIRA
except ImportError:                     # the `jira` extra is not installed
    JIRA = None

TEST_MARKER = '[TEST]'
TEST_LABEL = 'automation-test-DELETE-ME'
ENABLE_VAR = 'BUG_CREATION_ENABLED'


def creation_enabled() -> bool:
    """True when this environment permits a real Jira write."""
    return os.environ.get(ENABLE_VAR, 'false').lower() in ('1', 'true', 'yes')


class JiraClient:
    """Creates issues in Jira, or describes what it would create."""

    def __init__(self, server: str | None = None, user: str | None = None,
                 token: str | None = None, field_mapper: Any = None):
        self.server = server or os.environ.get('JIRA_URL')
        self.user = user or os.environ.get('JIRA_EMAIL')
        self.token = token or os.environ.get('JIRA_API_TOKEN')
        self.mapper = field_mapper if field_mapper is not None else default_mapper

    # -- field mapping ------------------------------------------------------

    def _map_fields(self, qa_touch_data: dict[str, Any] | None,
                    issue_type: str) -> tuple[dict[str, Any], list[str]]:
        """(mapped fields, required field names still unfilled)."""
        if not qa_touch_data or not self.mapper or issue_type not in BUG_LIKE:
            return {}, []
        stale = staleness_warning()
        if stale:
            log.warning('[JIRA] %s', stale)
        mapped = self.mapper.map_bug_fields(**qa_touch_data)
        return mapped, self.mapper.get_unmapped_required_fields(issue_type, mapped)

    # -- the dry run --------------------------------------------------------

    def _dry_run(self, summary: str, description: str, labels: list[str] | None,
                 issue_type: str, qa_touch_data: dict[str, Any] | None,
                 reason: str) -> dict[str, Any]:
        log.info('[JIRA] %s. Would create: %s', reason, summary)
        result: dict[str, Any] = {
            'key': 'AUTO-0001',
            'dry_run': True,
            'reason': reason,
            'summary': summary,
            'description': description,
            'labels': self._labels_for(summary, labels),
            'created_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        }
        mapped, unmapped = self._map_fields(qa_touch_data, issue_type)
        if mapped or unmapped:
            result['mapped_fields'] = mapped
        if unmapped:
            log.info('[JIRA] Unmapped required fields: %s', unmapped)
            result['unmapped_required_fields'] = unmapped
        return result

    @staticmethod
    def _labels_for(summary: str, labels: list[str] | None) -> list[str]:
        """The labels an issue carries, with the test marker when it applies.

        A "[TEST]" issue always carries the delete-me label, whether or not
        anything else was passed and whether or not a story was linked.
        """
        out = list(labels or [])
        if TEST_MARKER in summary and TEST_LABEL not in out:
            out.append(TEST_LABEL)
        return out

    # -- the real create ----------------------------------------------------

    def create_issue(self, summary: str, description: str,
                     labels: list[str] | None = None,
                     project_key: str | None = None,
                     issue_type: str = 'Bug',
                     attachments: list[str] | None = None,
                     story_link: str | None = None,
                     qa_touch_data: dict[str, Any] | None = None) -> dict[str, Any]:
        """Create a Jira issue, or describe the one that would be created.

        Always returns a dict with a `key`. A caller that reads
        `result['key']` never meets a None, which is what the original did
        on any real create without a story link.

        `qa_touch_data` carries the test's own words for the mandatory Bug
        fields: module, steps, expected, actual, environment, severity,
        build, title, test_case_id.
        """
        project_key = project_key or os.environ.get('JIRA_PROJECT_KEY')

        if not creation_enabled():
            return self._dry_run(summary, description, labels, issue_type,
                                 qa_touch_data, 'bug creation is disabled')
        if JIRA is None:
            return self._dry_run(summary, description, labels, issue_type,
                                 qa_touch_data,
                                 'the jira package is not installed')

        jira = JIRA({'server': self.server}, basic_auth=(self.user, self.token))
        fields: dict[str, Any] = {
            'project': {'key': project_key},
            'summary': summary,
            'description': description,
            'issuetype': {'name': issue_type},
        }
        issue_labels = self._labels_for(summary, labels)
        if issue_labels:
            fields['labels'] = issue_labels

        mapped, unmapped = self._map_fields(qa_touch_data, issue_type)
        fields.update(mapped)
        if unmapped:
            log.warning('[JIRA] These required fields were not auto-populated: %s',
                        unmapped)

        issue = jira.create_issue(fields=fields)
        issue_key = issue.key

        # Everything past this point is best effort. The issue exists; a
        # failure to decorate it must not lose its key, or the caller files
        # the same bug again.
        self._attach(jira, issue_key, attachments)
        if story_link:
            self._link_story(jira, issue, story_link)
        if TEST_MARKER in summary:
            self._mark_test_issue(jira, issue, issue_key)

        result = {'key': issue_key, 'self': getattr(issue, 'self', None),
                  'dry_run': False}
        if unmapped:
            result['unmapped_required_fields'] = unmapped
        return result

    # -- best-effort decoration --------------------------------------------

    @staticmethod
    def _attach(jira: Any, issue_key: str, attachments: list[str] | None) -> None:
        for path in attachments or []:
            try:
                if Path(path).exists():
                    jira.add_attachment(issue=issue_key, attachment=path)
            except Exception as exc:
                log.warning('Failed to attach %s: %s', path, exc)

    @staticmethod
    def _link_story(jira: Any, issue: Any, story_link: str) -> None:
        try:
            jira.add_comment(issue, f'Related story: {story_link}')
        except Exception as exc:
            log.warning('Failed to link story %s: %s', story_link, exc)

    @staticmethod
    def _mark_test_issue(jira: Any, issue: Any, issue_key: str) -> None:
        """Label a "[TEST]" issue for deletion and say so in a comment.

        Without this a test bug is indistinguishable from a real one in
        somebody else's backlog.
        """
        try:
            jira.add_comment(issue, 'Admin: Please delete this test bug')
        except Exception:
            log.warning('Failed to add the admin deletion comment to %s', issue_key)
        try:
            current = list(getattr(issue.fields, 'labels', []) or [])
            if TEST_LABEL not in current:
                current.append(TEST_LABEL)
                issue.update(fields={'labels': current})
        except Exception:
            log.warning('Failed to add the %s label to %s', TEST_LABEL, issue_key)
