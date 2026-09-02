"""Frozen shapes for the two JSON artifacts a test session leaves behind.

Both files are per-repo and gitignored, but they are read by tools this
package does not own - each repo's `run_archive`, `run_report`, `run_suite`
and `validate_run`, plus the retry CLI here. Renaming a field or dropping
one is therefore a breaking change to code that lives somewhere else, and
the reader that breaks does so quietly: it reads a missing key as "no
outcome" and reports a smaller run rather than an error.

The names below are the contract. Tests assert the writers emit exactly
these, so a rename fails in this package rather than in a consumer weeks
later.
"""
from __future__ import annotations

# logs/last_run_results.json
#
# `armed` tells "never intended to upload" (run_key None, armed False)
# from "meant to and could not". Readers .get() it, so older files
# still read.
RUN_RESULTS_FIELDS = ('timestamp', 'run_key', 'project_key', 'env', 'armed',
                      'exit', 'results')
RUN_RESULT_RECORD_FIELDS = ('nodeid', 'outcome', 'duration', 'case_key')

# logs/upload_failures.json - a LIST of these entries, appended to.
FAILURE_ENTRY_FIELDS = ('timestamp', 'project_key', 'run_key', 'result_count',
                        'results', 'comment', 'error')
# Each item of a failure entry's `results`. The legacy (status-only) upload
# path emits exactly these two; the playwright path adds execution_time,
# comments, description and attachments, which the retry CLI ignores.
FAILURE_RESULT_FIELDS = ('case', 'status')
