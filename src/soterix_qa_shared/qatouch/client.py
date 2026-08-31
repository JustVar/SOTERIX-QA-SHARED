"""QA Touch API client wrapper.

Endpoint inventory. Dates are when each was last confirmed against the live
API; per-method docstrings carry the measured detail and are authoritative.

  Read:
    GET  /api/v1/getAllProjects
    GET  /api/v1/getAllTestCases/{project_key}?page=N       (50/page)
    GET  /api/v1/getAllModules/{project_key}?page=N         (20/page)
    GET  /api/v1/getAllTestRuns/{project_key}               (20/page)
    GET  /api/v1/testRunResults/{project_key}/{run_key}     (100/page)
    GET  /api/v1/testRun/availableUsers/{project_key}
    GET  /api/v1/getTestRunlist/{case_key}                  (runs for one case)
  Write - Test Runs:
    POST /api/v1/testRun                                    (all cases)
    POST /api/v1/testRun/specific                           (specific case IDs)
    POST /api/v1/testRun/specific/module/mode               (by module + mode)
    POST /api/v1/clone/testRun                              (clone existing run)
  Write - Entities:
    POST /api/v1/project                                 (create project)
    POST /api/v1/module                                  (create module)
    POST /api/v1/testCase                                (create case, 02/19)
    POST /api/v1/testCase/steps                          (create STEP case, 08/27)
    POST /api/v1/testcase/update                         (partial case update)
    POST /api/v1/milestone                               (create release/milestone)
  Results:
    POST  /api/v1/testRunResults/testrun/code            (PREFERRED writer:
          the only endpoint whose response names the case it changed)
    PATCH /api/v1/testRunResults/status/multiple         (legacy)

  Paths that 404 - do not use: /api/v1/projects and /api/v1/modules (plural).
  create_project and create_module call the singular forms above.

  Not available via API:
    - Delete test cases or runs (UI only)
    - Update case tags (UI only; title/type/mode DO update via
      update_test_case)
    - List releases/milestones (creation works, list returns empty)

  API Limitations on Case Creation:
    - Only reliably sets: projectKey, sectionKey, caseTitle, description, precondition
    - Type, Priority, Mode, Tags are IGNORED (default to Acceptance/Critical)
    - Response does NOT return case_key (must fetch from list to find it)
    - Set Mode=Automation and Tags via QA Touch UI after creation
    - The plain /testCase endpoint makes the case default to the
      "Exploratory Session" template on some accounts, and those cases do
      NOT appear in getAllTestCases. Use create_test_case_with_steps for
      step cases ("Test case (Steps)" template).
    - getAllTestCases does not return cases from nested sub-modules (for
      example Admin > Integrations). They exist and show in the UI but are
      invisible to find_case_by_title; read a created case_key from the
      QA Touch case URL when the list endpoint cannot see it.

Auth: headers api-token + domain.  Base URL: https://api.qatouch.com
Rate limit: 50 requests/minute (HTTP 429 on excess).
"""
from __future__ import annotations

import json as _json
import os
from typing import Any, ClassVar

import requests

# QA Touch status codes for test run results
# Verified against GET /api/v1/testRuns/getAvailableStatuses on 2026-08-17.
STATUS_PASSED = 1
STATUS_UNTESTED = 2
STATUS_BLOCKED = 3   # also used for skipped
STATUS_RETEST = 4
STATUS_FAILED = 5
STATUS_NOT_APPLICABLE = 6
STATUS_IN_PROGRESS = 7
STATUS_HOLD = 8

# QA Touch spells the same status two ways depending on the endpoint:
#   getAvailableStatuses  -> "Not-Applicable", "In-Progress"  (hyphens)
#   test run result reads -> "Not Applicable", "In Progress"  (spaces)
# Measured 2026-08-17. Compare statuses through normalize_status_name(), never
# with ==, or the comparison is right only by luck.
STATUS_NAMES = {
    1: 'Passed', 2: 'Untested', 3: 'Blocked', 4: 'Retest',
    5: 'Failed', 6: 'Not Applicable', 7: 'In Progress', 8: 'Hold',
}


def normalize_status_name(status: str) -> str:
    """Fold a status name to one comparable form.

    "Not-Applicable", "Not Applicable" and "not applicable" all become
    "not applicable". Runs of whitespace collapse to one space.
    """
    return ' '.join(str(status or '').replace('-', ' ').split()).lower()


def status_code_for(status: str) -> int | None:
    """Return the numeric code for a status name in either spelling."""
    wanted = normalize_status_name(status)
    for code, name in STATUS_NAMES.items():
        if normalize_status_name(name) == wanted:
            return code
    return None


def case_code_to_ref(case_code: str) -> str:
    """Convert a QA Touch case code to the ref the results API expects.

    "TR0262" -> "262". The API wants the numeric part with no prefix and no
    leading zeros. See update_test_run_results for why this matters.
    """
    digits = ''.join(ch for ch in str(case_code) if ch.isdigit())
    return digits.lstrip('0') or '0'


PYTEST_TO_QATOUCH = {
    'passed': STATUS_PASSED,
    'failed': STATUS_FAILED,
    'skipped': STATUS_BLOCKED,
}


class QATouchError(Exception):
    pass


# QA Touch renamed the test-case fields in its public API some time after
# 2026-03-27. The old names are still in every snapshot under
# the UI repo's config/qatouch_snapshots/ and in the tools that read them, so a rename
# would silently break every one of those tools - they would read a missing
# key and quietly fall back to "Unknown". Rather than chase the rename
# through each tool, every case record carries both names.
#
# The rename is NOT in the public API docs - doc.qatouch.com/openapi.yaml
# documents no test-case response fields at all - so only a live check
# catches the next one.
#
# Old name -> new name.
LEGACY_CASE_FIELDS = {
    'module name': 'module_name',
    'created by': 'created_by',
    'created date': 'created_date',
    'updated date': 'updated_date',
}


def add_legacy_case_aliases(case: dict[str, Any]) -> dict[str, Any]:
    """Add the pre-rename field names to a test case record.

    Leaves any name the API already supplies untouched.
    """
    for old_name, new_name in LEGACY_CASE_FIELDS.items():
        if old_name not in case and new_name in case:
            case[old_name] = case[new_name]

    # Tags moved from a comma-joined string ("NewTags") to a list ("Tags").
    if 'NewTags' not in case:
        tags = case.get('Tags')
        if isinstance(tags, list):
            case['NewTags'] = ', '.join(str(t) for t in tags)
        elif isinstance(tags, str):
            case['NewTags'] = tags
        else:
            case['NewTags'] = ''
    return case


class QATouchClient:
    # QA Touch API requires: base URL = https://api.qatouch.com
    # Headers: api-token (API key) + domain (subdomain, e.g. 'soterixsystems')
    API_BASE: ClassVar[str] = 'https://api.qatouch.com'

    def __init__(self, api_key: str | None = None, domain: str | None = None,
                 base_url: str | None = None, timeout: int = 30):
        self.api_key = api_key or os.environ.get('QATOUCH_API_KEY')
        self.domain = domain or os.environ.get('QATOUCH_DOMAIN')
        # Legacy: accept base_url for backward compatibility but prefer API_BASE
        self.base_url = base_url or self.API_BASE
        if not self.api_key:
            raise QATouchError('QATOUCH_API_KEY must be provided (env or arg)')
        if not self.domain:
            raise QATouchError('QATOUCH_DOMAIN must be provided (env or arg, e.g. "soterixsystems")')
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            'Content-Type': 'application/json',
            'api-token': self.api_key,
            'domain': self.domain,
        })

    def _url(self, path: str) -> str:
        return self.base_url.rstrip('/') + path

    # ------------------------------------------------------------------ #
    #  Projects
    # ------------------------------------------------------------------ #

    def list_projects(self) -> list[dict[str, Any]]:
        url = self._url('/api/v1/getAllProjects')
        resp = self.session.get(url, timeout=self.timeout)
        if resp.status_code != 200:
            raise QATouchError(f'list_projects failed: {resp.status_code} {resp.text}')
        return resp.json()

    def create_project(self, name: str, code: str = '',
                       description: str = '') -> dict[str, Any]:
        """Create a project.

        POST /api/v1/project?name={name}

        The public API takes the project name as a query parameter and nothing
        else, so `code` and `description` are accepted for backward
        compatibility but are not sent. Set them in the QA Touch UI.

        Was previously POST /api/v1/projects with a JSON body. That path does
        not exist - it returns 404 - so this call never worked. Untested
        against live QA Touch, because testing it creates a real project.
        """
        url = self._url('/api/v1/project')
        resp = self.session.post(url, params={'name': name}, timeout=self.timeout)
        if resp.status_code not in (200, 201):
            raise QATouchError(f'create_project failed: {resp.status_code} {resp.text}')
        return resp.json()

    # ------------------------------------------------------------------ #
    #  Modules
    # ------------------------------------------------------------------ #

    def create_module(self, project_key: str, name: str,
                      parent_key: str | None = None) -> dict[str, Any]:
        """Create a module, optionally under a parent module.

        POST /api/v1/module?projectKey={key}&moduleName={name}[&parentKey={key}]

        Takes a project KEY (e.g. 'JlW1'), not a numeric id, and a parent
        module KEY for sub-modules.

        Was previously POST /api/v1/modules with a JSON body and numeric ids.
        That path does not exist - it returns 404 - so this call never worked.
        """
        params = {'projectKey': project_key, 'moduleName': name}
        if parent_key:
            params['parentKey'] = parent_key
        url = self._url('/api/v1/module')
        resp = self.session.post(url, params=params, json={}, timeout=self.timeout)
        if resp.status_code not in (200, 201):
            raise QATouchError(f'create_module failed: {resp.status_code} {resp.text}')
        return resp.json()

    def list_modules(self, project_key: str, page: int = 1) -> dict[str, Any]:
        """Return one page (20 items) of modules.

        Each module has: section_name, section_key.
        """
        url = self._url(f'/api/v1/getAllModules/{project_key}?page={page}')
        resp = self.session.get(url, timeout=self.timeout)
        if resp.status_code != 200:
            raise QATouchError(f'list_modules failed: {resp.status_code} {resp.text}')
        return resp.json()

    def list_all_modules(self, project_key: str) -> list[dict[str, Any]]:
        """Paginate through ALL modules in the project."""
        all_modules: list[dict[str, Any]] = []
        page = 1
        while True:
            data = self.list_modules(project_key, page=page)
            batch = data.get('data', [])
            if not batch:
                break
            all_modules.extend(batch)
            page += 1
        return all_modules

    # ------------------------------------------------------------------ #
    #  Test Cases
    # ------------------------------------------------------------------ #

    def create_test_case(
        self,
        project_key: str,
        section_key: str,
        title: str,
        description: str = '',
        precondition: str = '',
    ) -> dict[str, Any]:
        """Create a new test case in QA Touch.

        Args:
            project_key: Project key, e.g. "JlW1"
            section_key: Module/section key (from list_all_modules), e.g. "X4x9a"
            title: Test case title
            description: Optional description
            precondition: Optional precondition text

        Returns:
            API response dict. Note: does NOT contain case_key.
            Use find_case_by_title() after creation to get the key.

        Limitations:
            Type, Priority, Mode, and Tags cannot be set via API.
            They must be updated in the QA Touch UI after creation.
        """
        url = self._url('/api/v1/testCase')
        payload = {
            'projectKey': project_key,
            'sectionKey': section_key,
            'caseTitle': title,
        }
        if description:
            payload['description'] = description
        if precondition:
            payload['precondition'] = precondition
        resp = self.session.post(url, json=payload, timeout=self.timeout)
        if resp.status_code not in (200, 201):
            raise QATouchError(f'create_test_case failed: {resp.status_code} {resp.text}')
        data = resp.json()
        if not data.get('success', True):
            raise QATouchError(f'create_test_case error: {data.get("error_msg", data)}')
        return data

    def create_test_case_with_steps(
        self,
        project_key: str,
        section_key: str,
        title: str,
        steps: list[tuple[str, str]],
        description: str = '',
        precondition: str = '',
        reference: str = '',
        estimate: str = '',
    ) -> dict[str, Any]:
        """Create a step-based test case (template "Test case (Steps)").

        Use this, not create_test_case: the plain /testCase endpoint makes
        the account default to the "Exploratory Session" template, and those
        cases do not show in getAllTestCases. This posts to /testCase/steps
        with the steps_template shape QA Touch expects, matching the
        official qatouch-mcp-server.

        Args:
            steps: ordered list of (step_text, expected_result) pairs.
                   QA Touch expects at least one; keep expected non-empty.

        Returns the API response. As with create_test_case it does not echo
        the case_key; use find_case_by_title() afterwards to read it.
        """
        if not steps:
            raise QATouchError('create_test_case_with_steps needs at least one step')
        steps_template = {
            str(i): {'steps': step, 'expected_result': expected}
            for i, (step, expected) in enumerate(steps)
        }
        params = {
            'projectKey': project_key,
            'sectionKey': section_key,
            'caseTitle': title,
            'description': description,
            'precondition': precondition,
            'reference': reference,
            'estimate': estimate,
            'steps_template': _json.dumps(steps_template),
        }
        url = self._url('/api/v1/testCase/steps')
        resp = self.session.post(url, params=params, timeout=self.timeout)
        if resp.status_code not in (200, 201):
            raise QATouchError(
                f'create_test_case_with_steps failed: {resp.status_code} {resp.text}')
        data = resp.json()
        if not data.get('success', True):
            raise QATouchError(
                f'create_test_case_with_steps error: {data.get("error_msg", data)}')
        return data

    def find_case_by_title(
        self,
        project_key: str,
        title: str,
        section_key: str | None = None,
    ) -> dict[str, Any] | None:
        """Find a test case by exact title match. Returns the case dict or None.

        Searches from the last page backwards (newest cases first) for efficiency
        since newly created cases appear at the end.
        """
        # Get total count from first page meta
        first_page = self.list_test_cases(project_key, page=1)
        meta = first_page.get('meta', {})
        total = meta.get('total', 0)
        per_page = 50
        last_page = (total + per_page - 1) // per_page if total else 1

        # Search backwards from last page
        for page_num in range(last_page, max(last_page - 3, 0), -1):
            data = self.list_test_cases(project_key, page=page_num)
            for case in data.get('data', []):
                if case.get('case_title') == title:
                    if section_key and case.get('module_key') != section_key:
                        continue
                    return case
        return None

    def list_test_cases(self, project_key: str, page: int = 1,
                        module_key: str | None = None) -> dict[str, Any]:
        """Return one page (50 items) of test cases.

        Response: {"link": ..., "meta": {"total": N, ...}, "data": [...]}
        Each case has: case_title, case_key, module_key, module_name,
        Priority, Type, Tags, created_date, etc.

        Every case also carries the legacy key names ("module name", "NewTags",
        "created date", ...). See add_legacy_case_aliases for why.

        module_key filters server-side, which is far cheaper than pulling
        every page and filtering here. The API rate-limits bursts.
        """
        url = self._url(f'/api/v1/getAllTestCases/{project_key}?page={page}')
        if module_key:
            url += f'&moduleKey={module_key}'
        resp = self.session.get(url, timeout=self.timeout)
        if resp.status_code != 200:
            raise QATouchError(f'list_test_cases failed: {resp.status_code} {resp.text}')
        payload = resp.json()
        payload['data'] = [add_legacy_case_aliases(c) for c in payload.get('data', [])]
        return payload

    def list_all_test_cases(self, project_key: str,
                            module_key: str | None = None) -> list[dict[str, Any]]:
        """Paginate through ALL test cases in the project. Returns flat list.

        Pass module_key to fetch one module only.
        """
        all_cases: list[dict[str, Any]] = []
        page = 1
        while True:
            data = self.list_test_cases(project_key, page=page, module_key=module_key)
            batch = data.get('data', [])
            if not batch:
                break
            all_cases.extend(batch)
            page += 1
        return all_cases

    def get_case_test_runs(self, case_key: str) -> list[dict[str, Any]]:
        """Return every test run that contains this test case.

        GET /api/v1/getTestRunlist/{caseKey}

        Each entry carries testrunkey, resultKey, name, status and
        Assigned_To. resultKey is what the run-result status endpoints need,
        so this is the cheap way to find a case inside existing runs - one
        call per case instead of paging every run's results.
        """
        url = self._url(f'/api/v1/getTestRunlist/{case_key}')
        resp = self.session.get(url, timeout=self.timeout)
        if resp.status_code != 200:
            raise QATouchError(f'get_case_test_runs failed: {resp.status_code} {resp.text}')
        detail = resp.json().get('testrundetail') or []
        # The API nests this list one level deeper than the other endpoints.
        flat: list[dict[str, Any]] = []
        for entry in detail:
            if isinstance(entry, list):
                flat.extend(entry)
            elif isinstance(entry, dict):
                flat.append(entry)
        return flat

    # ------------------------------------------------------------------ #
    #  Users
    # ------------------------------------------------------------------ #

    def list_available_users(self, project_key: str) -> list[dict[str, Any]]:
        """List users available for test run assignment in a project.

        Returns list of: {"user_key": "d6JZ", "name": "...", "email": "..."}
        The user_key is required for assignTo in test run creation.
        """
        url = self._url(f'/api/v1/testRun/availableUsers/{project_key}')
        resp = self.session.get(url, timeout=self.timeout)
        if resp.status_code != 200:
            raise QATouchError(f'list_available_users failed: {resp.status_code} {resp.text}')
        data = resp.json()
        return data.get('data', [])

    # ------------------------------------------------------------------ #
    #  Test Runs - read
    # ------------------------------------------------------------------ #

    def list_test_runs(self, project_key: str) -> list[dict[str, Any]]:
        """List all test runs for a project. Returns list (may be empty).

        Each run has: testrun_key, Name, Description, Version, Type,
        Assigned_To, Browser, Release Name, Status, Created Date, Updated Date.
        """
        url = self._url(f'/api/v1/getAllTestRuns/{project_key}')
        resp = self.session.get(url, timeout=self.timeout)
        if resp.status_code != 200:
            raise QATouchError(f'list_test_runs failed: {resp.status_code} {resp.text}')
        data = resp.json()
        # API returns {"msg": "No Test Run(s) found..."} when empty
        if isinstance(data, dict) and 'msg' in data and 'data' not in data:
            return []
        return data.get('data', data if isinstance(data, list) else [])

    def get_test_run_results(self, project_key: str, run_key: str,
                             page: int = 1) -> dict[str, Any]:
        """Get test case results for a specific test run (100/page).

        Each result has: run_key, case_id, title, status, Created Date,
        assigned_user, code.
        """
        url = self._url(f'/api/v1/testRunResults/{project_key}/{run_key}?page={page}')
        resp = self.session.get(url, timeout=self.timeout)
        if resp.status_code != 200:
            raise QATouchError(
                f'get_test_run_results failed: {resp.status_code} {resp.text}')
        return resp.json()

    def get_all_test_run_results(self, project_key: str,
                                 run_key: str) -> list[dict[str, Any]]:
        """Paginate through ALL results for a test run. Returns flat list."""
        all_results: list[dict[str, Any]] = []
        page = 1
        while True:
            data = self.get_test_run_results(project_key, run_key, page=page)
            batch = data.get('data', [])
            if not batch:
                break
            all_results.extend(batch)
            page += 1
        return all_results

    # ------------------------------------------------------------------ #
    #  Milestones / Releases
    # ------------------------------------------------------------------ #

    def create_milestone(
        self,
        project_key: str,
        name: str,
    ) -> dict[str, Any]:
        """Create a new milestone (release) in QA Touch.

        Args:
            project_key: Project key, e.g. "JlW1"
            name: Milestone name, e.g. "v1.7.0 - Stage"

        Returns:
            {"success": true, "data": [{"milestone_id": "vqQM", ...}]}
        """
        url = self._url('/api/v1/milestone')
        payload = {
            'projectKey': project_key,
            'milestone': name,
        }
        resp = self.session.post(url, json=payload, timeout=self.timeout)
        if resp.status_code not in (200, 201):
            raise QATouchError(f'create_milestone failed: {resp.status_code} {resp.text}')
        data = resp.json()
        if not data.get('success', True):
            raise QATouchError(f'create_milestone error: {data.get("error_msg", data)}')
        return data

    # ------------------------------------------------------------------ #
    #  Test Runs - create
    # ------------------------------------------------------------------ #

    def create_test_run_all(
        self,
        project_key: str,
        name: str,
        assign_to: str,
        milestone_key: str,
    ) -> dict[str, Any]:
        """Create a test run with ALL test cases in the project.

        WARNING: Slow for large projects (2,800+ cases can take >60s).

        Args:
            project_key: Project key, e.g. "JlW1"
            name: Test run name, e.g. "Regression - Stage - v1.6.0 - 2026-02-20"
            assign_to: User key from list_available_users(), e.g. "d6JZ"
            milestone_key: Release/milestone key from QA Touch UI, e.g. "Dmn7"

        Returns:
            {"success": true, "data": [{"testrun_id": "...", ...}]}
        """
        url = self._url('/api/v1/testRun')
        payload = {
            'projectKey': project_key,
            'assignTo': assign_to,
            'milestoneKey': milestone_key,
            'testRun': name,
        }
        resp = self.session.post(url, json=payload, timeout=120)
        if resp.status_code not in (200, 201):
            raise QATouchError(f'create_test_run_all failed: {resp.status_code} {resp.text}')
        data = resp.json()
        if not data.get('success', True):
            raise QATouchError(f'create_test_run_all error: {data.get("error_msg", data)}')
        return data

    def create_test_run(
        self,
        project_key: str,
        name: str,
        assign_to: str,
        milestone_key: str,
        case_ids: list[str],
    ) -> dict[str, Any]:
        """Create a test run with specific test cases.

        Args:
            project_key: Project key, e.g. "JlW1"
            name: Test run name, e.g. "Smoke - Stage - v1.6.0 - 2026-02-20"
            assign_to: User key from list_available_users(), e.g. "d6JZ"
            milestone_key: Release/milestone key, e.g. "Dmn7"
            case_ids: List of test case keys, e.g. ["5BRMnj", "21zWnm"]

        Returns:
            {"success": true, "data": [{"testrun_id": "XGbg0", ...}]}
        """
        url = self._url('/api/v1/testRun/specific')
        payload = {
            'projectKey': project_key,
            'assignTo': assign_to,
            'milestoneKey': milestone_key,
            'testRun': name,
            'caseId': case_ids,
        }
        resp = self.session.post(url, json=payload, timeout=120)
        if resp.status_code not in (200, 201):
            raise QATouchError(f'create_test_run failed: {resp.status_code} {resp.text}')
        data = resp.json()
        if not data.get('success', True):
            raise QATouchError(f'create_test_run error: {data.get("error_msg", data)}')
        return data

    def create_test_run_by_tag(
        self,
        project_key: str,
        name: str,
        assign_to: str,
        milestone_key: str,
        tag: str,
    ) -> dict[str, Any]:
        """Create a test run with all cases matching a tag.

        Args:
            project_key: Project key, e.g. "JlW1"
            name: Test run name, e.g. "Smoke - Stage - v1.6.0"
            assign_to: User key from list_available_users(), e.g. "d6JZ"
            milestone_key: Release/milestone key, e.g. "Dmn7"
            tag: Tag name as a string, e.g. "smoke"
                 IMPORTANT: Must be a string, NOT an array. Array causes 500 error.

        Returns:
            {"success": true, "data": [{"testrun_id": "...", ...}]}
        """
        url = self._url('/api/v1/testRun/specific')
        payload = {
            'projectKey': project_key,
            'assignTo': assign_to,
            'milestoneKey': milestone_key,
            'testRun': name,
            'tags': tag,  # Must be string, not array
        }
        resp = self.session.post(url, json=payload, timeout=120)
        if resp.status_code not in (200, 201):
            raise QATouchError(f'create_test_run_by_tag failed: {resp.status_code} {resp.text}')
        data = resp.json()
        if not data.get('success', True):
            raise QATouchError(f'create_test_run_by_tag error: {data.get("error_msg", data)}')
        return data

    def create_test_run_by_module(
        self,
        project_key: str,
        name: str,
        assign_to: str,
        milestone_key: str,
        module_keys: list[str],
        mode: str | None = None,
    ) -> dict[str, Any]:
        """Create a test run with all cases from specific modules.

        Args:
            project_key: Project key, e.g. "JlW1"
            name: Test run name
            assign_to: User key from list_available_users()
            milestone_key: Release/milestone key
            module_keys: List of module section_key values
            mode: Optional filter -- "automation" or "manual"

        Returns:
            {"success": true, "data": [{"testrun_id": "...", ...}]}
        """
        url = self._url('/api/v1/testRun/specific/module/mode')
        payload = {
            'projectKey': project_key,
            'assignTo': assign_to,
            'milestoneKey': milestone_key,
            'testRun': name,
            'moduleKey': module_keys,
        }
        if mode:
            payload['mode'] = mode
        resp = self.session.post(url, json=payload, timeout=120)
        if resp.status_code not in (200, 201):
            raise QATouchError(
                f'create_test_run_by_module failed: {resp.status_code} {resp.text}')
        data = resp.json()
        if not data.get('success', True):
            raise QATouchError(
                f'create_test_run_by_module error: {data.get("error_msg", data)}')
        return data

    def clone_test_run(
        self,
        project_key: str,
        source_run_key: str,
        new_name: str,
    ) -> dict[str, Any]:
        """Clone an existing test run. All cases in the new run start as Untested.

        Args:
            project_key: Project key, e.g. "JlW1"
            source_run_key: Key of the run to clone, e.g. "vaXdn"
            new_name: Name for the cloned run

        Returns:
            {"success": true, "msg": "Test run cloned Successfully. Your new Testrun Key - zvXw9"}
        """
        url = self._url('/api/v1/clone/testRun')
        payload = {
            'projectKey': project_key,
            'testrunKey': source_run_key,
            'testrunName': new_name,
        }
        resp = self.session.post(url, json=payload, timeout=60)
        if resp.status_code not in (200, 201):
            raise QATouchError(f'clone_test_run failed: {resp.status_code} {resp.text}')
        data = resp.json()
        if not data.get('success', True):
            raise QATouchError(f'clone_test_run error: {data.get("error_msg", data)}')
        return data

    # ------------------------------------------------------------------ #
    #  Test run result updates
    # ------------------------------------------------------------------ #

    # Field name -> the query parameter the update endpoint expects.
    CASE_UPDATE_FIELDS: ClassVar[dict] = {
        'title': 'caseTitle',
        'caseTitle': 'caseTitle',
        'description': 'description',
        'precondition': 'precondition',
        'reference': 'reference',
        'estimate': 'estimate',
        'type': 'type',
        'priority': 'priority',
        'mode': 'mode',
        # sectionKey is deliberately absent. The endpoint documents it, accepts
        # it and answers {"success": true} - and the case does not move.
        # Measured 2026-08-17 by moving a case between two PTZ modules and
        # reading it back: it never left the source module. Module moves have
        # to go through the browser endpoint. tools/archive/reorganize_ldap_cases.py
        # is a worked example.
    }

    def update_test_case(self, project_key: str, case_key: str,
                         **fields: Any) -> dict[str, Any]:
        """Update fields on an existing test case through the public API.

        POST /api/v1/testcase/update

        Accepts title, description, precondition, reference, estimate, type,
        priority and mode.

        This is a PARTIAL update - measured 2026-08-17. Fields you leave out
        keep their values; only what you send changes. That is unlike the
        browser endpoint /v1/case/update, which needs the whole case echoed
        back and silently drops anything you forget.

        MODULE MOVES ARE NOT SUPPORTED either, whatever the docs imply - the
        sectionKey parameter is accepted and ignored.

        TAGS ARE NOT SUPPORTED. The public API has no way to change the tags
        on a case that already exists. Use tools/qatouch_suite_hold.py, which
        drives the browser session, for anything tag-related.

        Steps and BDD scripts have their own endpoints
        (/api/v1/testcase/steps/update and /api/v1/testcase/bdd/update) and
        are not handled here.
        """
        unknown = [k for k in fields if k not in self.CASE_UPDATE_FIELDS]
        if unknown:
            raise QATouchError(
                f'update_test_case: unsupported field(s) {unknown}. '
                f'Supported: {sorted(set(self.CASE_UPDATE_FIELDS))}. '
                f'Tags cannot be changed through the public API.')
        if not fields:
            raise QATouchError('update_test_case: nothing to update')

        params = {'projectKey': project_key, 'caseKey': case_key}
        for name, value in fields.items():
            params[self.CASE_UPDATE_FIELDS[name]] = value

        url = self._url('/api/v1/testcase/update')
        resp = self.session.post(url, params=params, timeout=self.timeout)
        if resp.status_code != 200:
            raise QATouchError(
                f'update_test_case failed: {resp.status_code} {resp.text[:300]}')
        data = resp.json()
        if not data.get('success', True):
            raise QATouchError(f'update_test_case API error: {data}')
        return data

    def verify_results_landed(self, project_key: str, test_run_key: str,
                              results: list[dict[str, Any]]) -> dict[str, Any]:
        """Read a run back and check that each result carries the status we sent.

        results: the same list handed to update_test_run_results_by_code -
                 [{"case": "TR0263", "status": 1}, ...]

        Returns {"checked": n, "landed": [...codes], "mismatched": [...],
                 "missing": [...codes not in the run],
                 "in_run": how many cases the run holds,
                 "uncovered": [...codes in the run this upload did not set]}.

        This exists because the results endpoints answer success for writes
        they discard. Every uploader in the repo calls this after writing;
        an upload is not done until the run agrees. Status names are compared
        through normalize_status_name because QA Touch spells them two ways.
        """
        rows = self.get_all_test_run_results(project_key, test_run_key)
        by_code = {r.get('code'): r.get('status') for r in rows}
        landed, mismatched, missing = [], [], []
        for entry in results:
            code = str(entry.get('case', ''))
            try:
                want = STATUS_NAMES.get(int(entry.get('status')))
            except (TypeError, ValueError):
                want = None
            got = by_code.get(code)
            if got is None:
                missing.append(code)
            elif want is None:
                # A status this client cannot name is a status it cannot
                # check. Counting it as landed was a hole in the one guard
                # the upload path trusts: measured, a status of 0, an
                # unknown code, or a missing key all reported LANDED while
                # the run still read Untested. The reporter can only send
                # 1, 3 or 5, but the retry CLI replays whatever is on disk.
                mismatched.append(
                    f'{code}: sent status {entry.get("status")!r}, which is '
                    f'not a QA Touch status, so the run cannot be checked')
            elif normalize_status_name(got) != normalize_status_name(want):
                mismatched.append(f'{code}: sent {want}, run shows {got}')
            else:
                landed.append(code)
        # Cases the run holds that this upload said nothing about. The rows
        # are already fetched, so this costs nothing, and it is the only
        # thing here that answers "is the run finished" rather than "did my
        # write land". Measured on the first real armed run: 4 of 5 results
        # landed, every check passed, and the run still held an Untested
        # case because the selected tests did not cover it. The summary
        # looked complete.
        sent = {str(e.get('case', '')) for e in results}
        uncovered = sorted(code for code in by_code
                           if code and code not in sent)
        return {'checked': len(results), 'landed': landed,
                'mismatched': mismatched, 'missing': missing,
                'in_run': len(by_code), 'uncovered': uncovered}

    def update_test_run_results_by_code(
        self,
        project_key: str,
        test_run_key: str,
        results: list[dict[str, Any]],
        comments: str = '',
        attachments: list[tuple[str, bytes, str]] | None = None,
    ) -> dict[str, Any]:
        """Set test run result statuses, keyed by full case CODE.

        POST /api/v1/testRunResults/testrun/code

        Args:
            project_key: e.g. "JlW1"
            test_run_key: e.g. "vaQly"
            results: list of {"case": "TR0263", "status": <int>} - the FULL
                     case code including the "TR" prefix.
            comments: optional comment applied to the update
            attachments: optional list of (filename, content, content_type)
                     sent as multipart "file[]" parts. The endpoint accepts
                     png/pdf/jpg/csv/log up to 2 MB per file. Callers enforce
                     the cap - nothing is checked or truncated here.

        Prefer this over update_test_run_results(). Two reasons:

        1. It takes the readable full case code, not the stripped numeric
           form, so the payload matches what the mapping and CSV exports hold.
        2. Its response names the case it changed -
           {"success": true, "msg": "TR0263 - Updated Successfully"} - so a
           no-op is visible. The status/multiple endpoint returns the same
           generic success whether or not anything changed.

        Verified against the live API 2026-08-17. The attachment path has
        NOT run against the live API yet - the next real upload with a
        failure is the verification.
        """
        payload = {str(i): r for i, r in enumerate(results)}
        params = {
            'project': project_key,
            'test_run': test_run_key,
            'result': _json.dumps(payload),
        }
        if comments:
            params['comments'] = comments
        url = self._url('/api/v1/testRunResults/testrun/code')
        if attachments:
            files = [('file[]', (name, content, content_type))
                     for name, content, content_type in attachments]
            # The session pins Content-Type: application/json for the JSON
            # endpoints. A multipart body must carry the boundary header
            # requests generates instead, so the session value is dropped
            # for this one request (a None header value removes it on merge).
            resp = self.session.post(url, params=params, files=files,
                                     headers={'Content-Type': None},
                                     timeout=self.timeout)
        else:
            # No attachments: this request is byte-identical to the
            # pre-attachment client, so plain status uploads cannot drift.
            resp = self.session.post(url, params=params, timeout=self.timeout)
        if resp.status_code != 200:
            raise QATouchError(
                f'update_test_run_results_by_code failed: {resp.status_code} {resp.text}'
            )
        data = resp.json()
        if not data.get('success', True):
            raise QATouchError(
                f'update_test_run_results_by_code API error: {data.get("error_msg", data)}'
            )
        return data

    def update_test_run_results(
        self,
        project_key: str,
        test_run_key: str,
        results: list[dict[str, Any]],
        comment: str = '',
    ) -> dict[str, Any]:
        """Bulk-update test run results.

        Args:
            project_key: e.g. "JlW1"
            test_run_key: e.g. "vaXdn"
            results: list of {"case": "<case ref>", "status": <int>}
                     The case ref is the CASE CODE with the "TR" prefix and
                     leading zeros removed - TR0262 becomes "262". Use
                     case_code_to_ref() to build it.
                     Status codes: 1=passed, 2=untested, 3=blocked/skipped,
                                   4=retest, 5=failed, 6=not applicable,
                                   7=in progress, 8=hold
            comment: optional comment to attach

        Uses: PATCH /api/v1/testRunResults/status/multiple

        WARNING - this endpoint fails silently. It answers
        {"success": true, "msg": "Updated Test Run's status"} even when the
        case ref matches nothing and no status changes. Measured 2026-08-17:
        the case key ("B869bm"), the full case code ("TR0262") and the
        per-run result key ("w5BNkb") were all accepted and all did nothing.
        Only the stripped code ("262") actually moved the status.
        Always read the status back to confirm a write landed.
        """
        url = self._url('/api/v1/testRunResults/status/multiple')
        params = {
            'project': project_key,
            'test_run': test_run_key,
            'result': _json.dumps(results),
        }
        if comment:
            params['comment'] = comment
        resp = self.session.patch(url, params=params, timeout=self.timeout)
        if resp.status_code != 200:
            raise QATouchError(
                f'update_test_run_results failed: {resp.status_code} {resp.text}'
            )
        data = resp.json()
        if not data.get('success', True):
            raise QATouchError(
                f'update_test_run_results API error: {data.get("error_msg", data)}'
            )
        return data
