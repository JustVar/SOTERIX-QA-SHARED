"""Where a consumer repo keeps the evidence the reporting stack reads and
writes.

One definition, because the archive plugin, the run-report writer, the
health summary and the media capture all touch the same directories. If each
derived its own, a layout change would move one and not the others, and the
symptom would be a report that renders against a folder nothing wrote.

The package never guesses a root. `__file__` points into site-packages after
installation, and the health CLI runs with no pytest config at all, so the
root arrives from the caller: `config.rootpath` for the plugins,
`Path.cwd()` for a CLI run from the repo root. `paths_for` is the single
source; each module's `configure(root)` reads it.
"""
from __future__ import annotations

from pathlib import Path


def paths_for(root: Path | str) -> dict[str, Path]:
    """Every reporting path, derived from one repo root."""
    root = Path(root)
    reports = root / 'reports'
    logs = root / 'logs'
    return {
        'root': root,
        # Per-run evidence archive. Tracked by nobody; regenerable.
        'runs': reports / 'runs',
        # The latest run's live evidence, overwritten each run. The archive
        # copies these into a run folder before the next run wipes them.
        'screenshots': reports / 'screenshots',
        'artifacts': reports / 'artifacts',
        # The reporter's per-node-id record and the session log.
        'run_results': logs / 'last_run_results.json',
        'run_log': logs / 'run.log',
        # Read-only: case titles for the report, never written here.
        'mapping': root / 'config' / 'qatouch_case_mapping.json',
    }
