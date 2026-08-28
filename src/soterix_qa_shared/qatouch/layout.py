"""Where a consumer repo keeps the files this package reads and writes.

One definition, because more than one module needs it. The reporter writes
`upload_failures.json`; the retry CLI reads it, empties it, and writes the
unreplayable leftovers beside it. If each derived its own path, a change to
the layout would move one and not the other, and the symptom would be a
retry that quietly finds nothing to retry.

The package never guesses a root. `__file__` points into site-packages
after installation, and two consumer CLIs run with no pytest config at all,
so the root arrives from the caller: `config.rootpath` for the pytest
plugin, and the repo root a CLI resolves for itself.
"""
from __future__ import annotations

from pathlib import Path


def paths_for(root: Path | str) -> dict[str, Path]:
    """Every path this package touches, derived from one repo root."""
    root = Path(root)
    logs = root / 'logs'
    return {
        'root': root,
        'mapping': root / 'config' / 'qatouch_case_mapping.json',
        'logs': logs,
        'failure_log': logs / 'upload_failures.json',
        'unreplayable_log': logs / 'upload_failures_unreplayable.json',
        'run_results': logs / 'last_run_results.json',
        # Tracked in git, unlike everything above it. One short line per
        # armed run; see results.RunLedgerSink for why only armed runs.
        'history': root / 'history',
    }
