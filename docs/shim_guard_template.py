"""Guards for the soterix-qa-shared compatibility shims.

Copy this to each consuming repo as `tests/plugins/test_shims.py`, then edit
the two places marked THIS REPO.

A broken shim is silent. Replacing the module alias with a star-import
leaves the repo's own reporter suite fully green while every monkeypatch
seam stops reaching the code that actually runs, and the writer paths drift
back to whatever the package last set. These tests fail loudly in that case.

Four things are asserted:
  1. module identity - the old import path IS the package module;
  2. patching the old path reaches the running code;
  3. the writer paths resolve inside THIS repo, not into site-packages;
  4. a module with a CLI still runs its CLI through the shim.

Plus one each repo owns: the mapping policy its shim injects is the right
one (the UI repo gates on `reviewed`; the API repo does not).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from soterix_qa_shared.qatouch import client as pkg_client
from soterix_qa_shared.qatouch import reporter as pkg_reporter
from soterix_qa_shared.qatouch import retry as pkg_retry
from soterix_qa_shared.qatouch.results import RunResult, RunResultsFileSink
from soterix_qa_shared.qatouch.upload import QATouchUploadSink

from plugins import qatouch_reporter as shim_reporter
from tools import qatouch_client as shim_client
from tools import retry_uploads as shim_retry


def _real(p) -> Path:
    """Normalise both sides before comparing (Rule 7).

    On Windows the same directory is reachable as C:\\Users\\JUSTIN~1\\... and
    C:\\Users\\JustinVarughese\\...; pytest's rootpath and a test file's
    __file__ do not always agree on which. Comparing the raw strings gives a
    false failure.
    """
    return Path(os.path.realpath(str(p)))


# --- 1. module identity -----------------------------------------------------

def test_reporter_shim_is_the_package_module():
    assert shim_reporter is pkg_reporter, (
        'plugins.qatouch_reporter is not the package module. A star-import or '
        'a copied file leaves every monkeypatch seam pointing at a dead object.')


def test_client_shim_is_the_package_module():
    assert shim_client is pkg_client, (
        'tools.qatouch_client is not the package module.')


def test_retry_shim_is_the_package_module():
    assert shim_retry is pkg_retry, (
        'tools.retry_uploads is not the package module.')


def test_shim_is_registered_under_one_name_only(pytestconfig):
    """Rule 1: the package must not also arrive via an entry point.

    Listing both the shim path and the package path raises
    `ValueError: Plugin already registered under a different name` before any
    hook runs, so every session in the repo dies at configure time.
    """
    names = {
        name for name, plugin in pytestconfig.pluginmanager.list_name_plugin()
        if plugin is pkg_reporter
    }
    assert len(names) <= 1, f'reporter registered under several names: {names}'


# --- 2. patching the old path reaches the running code ----------------------

def test_patching_the_old_path_moves_the_run_record(monkeypatch, tmp_path):
    """The record sink is handed its path, but the plugin reads the module
    global to find it - so a patch on the OLD path must still land."""
    target = tmp_path / 'last_run_results.json'
    monkeypatch.setattr(shim_reporter, 'RUN_RESULTS_PATH', target)

    sink = RunResultsFileSink(shim_reporter.RUN_RESULTS_PATH, run_key='rk-shim-guard')
    sink.finish([RunResult(nodeid='tests/x.py::test_y', outcome='passed')])

    assert target.exists(), (
        'patching the old import path did not reach the writer - the shim is '
        'not an alias, or the module froze its path at import time.')
    assert json.loads(target.read_text())['run_key'] == 'rk-shim-guard'


def test_patching_the_old_path_moves_the_failure_log(monkeypatch, tmp_path):
    target = tmp_path / 'upload_failures.json'
    monkeypatch.setattr(shim_reporter, 'FAILURE_LOG_PATH', target)

    sink = QATouchUploadSink(run_key='rk-shim-guard',
                             failure_log_path=shim_reporter.FAILURE_LOG_PATH)
    sink._save_failed_results([{'case': 'TR1', 'status': 5}], 'c', 'e')

    assert target.exists(), 'the failure log ignored the patched path'


# --- 3. writer paths stay inside this repo ----------------------------------

@pytest.mark.parametrize('name', [
    'MAPPING_PATH', 'FAILURE_LOG_DIR', 'FAILURE_LOG_PATH', 'RUN_RESULTS_PATH',
])
def test_writer_paths_resolve_inside_this_repo(name):
    value = getattr(pkg_reporter, name)
    assert value is not None, (
        f'{name} is None - reporter.configure(root) never ran. The plugin '
        f'calls it from pytest_configure with config.rootpath.')
    assert _real(value).is_relative_to(_real(REPO_ROOT)), (
        f'{name} resolves to {value}, outside {REPO_ROOT}. After extraction a '
        f'__file__-relative default writes into site-packages, and --rootdir '
        f'silently relocates every derived path.')


def test_the_retry_shim_points_at_this_repos_failure_log():
    """The reporter writes it and the retry CLI reads it. If the two shims
    configure different roots, the retry finds nothing to retry and says so
    cheerfully."""
    assert pkg_retry.FAILURE_LOG_PATH is not None, (
        'retry.configure(root) never ran - the tools/retry_uploads.py shim '
        'must call it.')
    assert pkg_retry.FAILURE_LOG_PATH == pkg_reporter.FAILURE_LOG_PATH


def test_the_module_globals_are_read_at_call_time(monkeypatch, tmp_path):
    """Rule 6: a recompute-from-ROOT accessor ignores the patch, writes into
    the real repo, and the test that thought it was isolated still passes."""
    monkeypatch.setattr(shim_reporter, 'RUN_RESULTS_PATH', tmp_path / 'x.json')
    assert pkg_reporter.RUN_RESULTS_PATH == tmp_path / 'x.json'


# --- 4. a CLI still runs through its shim -----------------------------------

def test_the_retry_cli_actually_runs_through_the_shim():
    """Rule 2, the corrected version.

    A PLAIN alias shim makes this exit 0 having printed nothing and done
    nothing: the module body swaps sys.modules['__main__'] and falls off the
    end. Measured - a reader sees "no output, exit 0" and concludes there
    was nothing to retry while a real failure sits in the log. Only the
    guarded form, which calls main() BEFORE the alias swap, runs the CLI.
    """
    script = REPO_ROOT / 'tools' / 'retry_uploads.py'
    proc = subprocess.run(
        [sys.executable, str(script), '--dry-run'],
        capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=120)
    assert proc.stdout.strip(), (
        f'{script.name} --dry-run printed nothing. A plain alias shim exits 0 '
        f'having run no code at all; this module needs the guarded form.')


# --- 5. THIS REPO: the injected mapping policy ------------------------------
#
# Keep ONE of the two tests below and delete the other.
#   * The UI repo gates on `reviewed`, because most of its rows were
#     auto-matched by word overlap - proposals, not links.
#   * The API repo has no gate: every mapped row uploads.

def test_the_injected_policy_is_this_repos_gate(tmp_path):
    policy = pkg_reporter.MAPPING_POLICY
    assert policy is not None, (
        'no MAPPING_POLICY injected - the shim must set it before it swaps '
        'sys.modules, or every armed run degrades to no-upload.')

    mapping_file = tmp_path / 'qatouch_case_mapping.json'
    mapping_file.write_text(json.dumps({
        'version': 1,
        'mappings': {
            'tests/a.py::test_one': {'case_key': 'K1'},          # no reviewed key
            'tests/a.py::test_two': {'case_key': 'K2', 'reviewed': False},
            'tests/a.py::test_three': {'case_key': 'K3', 'reviewed': True},
        },
    }), encoding='utf-8')

    lookup, counts = policy(mapping_file)

    # --- THIS REPO: the UI variant (delete for the API repo) ---
    assert lookup == {'tests/a.py::test_three': 'K3'}, (
        'the review gate is not the injected policy. An unreviewed row is a '
        'proposal, and uploading through one writes a real status onto a '
        'case no test verifies.')
    assert counts['unreviewed'] == 2

    # --- THIS REPO: the API variant (delete for the UI repo) ---
    # A missing `reviewed` key must read as "not gated", never KeyError -
    # zero of the API repo's rows carry it.
    # assert lookup == {'tests/a.py::test_one': 'K1',
    #                   'tests/a.py::test_two': 'K2',
    #                   'tests/a.py::test_three': 'K3'}
