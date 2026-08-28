"""Guards for the soterix-qa-shared compatibility shims.

A broken shim is silent. Replacing the module alias with a star-import leaves
the reporter suite green while every monkeypatch seam stops reaching the code
that actually runs, and the writer paths drift back to whatever the package
last set. These tests fail loudly in that case.

Three things are asserted, per the migration plan:
  1. module identity - the old import path IS the package module;
  2. patching the old path reaches the running code;
  3. the writer paths resolve inside THIS repo, not into site-packages.

Plus one this repo owns: the mapping policy injected by its shim is the
permissive one (the review gate is the UI repo's).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from soterix_qa_shared.qatouch import client as pkg_client
from soterix_qa_shared.qatouch import reporter as pkg_reporter

from plugins import qatouch_reporter as shim_reporter
from tools import qatouch_client as shim_client


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


def test_shim_is_registered_under_one_name_only(pytestconfig):
    """Rule 1: the package must not also arrive via an entry point."""
    names = {
        name for name, plugin in pytestconfig.pluginmanager.list_name_plugin()
        if plugin is pkg_reporter
    }
    assert len(names) <= 1, f'reporter registered under several names: {names}'


# --- 2. patching the old path reaches the running code ----------------------

def test_patching_the_old_path_moves_the_run_results_writer(monkeypatch, tmp_path):
    target = tmp_path / 'last_run_results.json'
    monkeypatch.setattr(shim_reporter, 'FAILURE_LOG_DIR', tmp_path)
    monkeypatch.setattr(shim_reporter, 'RUN_RESULTS_PATH', target)

    reporter = pkg_reporter.QATouchReporter('rk-shim-guard')
    reporter.nodeid_outcomes = {
        'tests/x.py::test_y': {'outcome': 'passed', 'duration': 0.1, 'case_key': None},
    }
    reporter._write_run_results()

    assert target.exists(), (
        'patching the old import path did not reach the writer - the shim is '
        'not an alias, or the writer froze its path at import time.')
    assert json.loads(target.read_text())['run_key'] == 'rk-shim-guard'


def test_patching_the_old_path_moves_the_failure_log(monkeypatch, tmp_path):
    target = tmp_path / 'upload_failures.json'
    monkeypatch.setattr(shim_reporter, 'FAILURE_LOG_DIR', tmp_path)
    monkeypatch.setattr(shim_reporter, 'FAILURE_LOG_PATH', target)

    reporter = pkg_reporter.QATouchReporter('rk-shim-guard')
    reporter._save_failed_results([{'case': 'TR1', 'status': 5}], 'c', 'e')

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
        f'__file__-relative default writes into site-packages.')


def test_accessors_read_the_module_global_at_call_time(monkeypatch, tmp_path):
    """Rule 6: a recompute-from-ROOT accessor ignores the patch."""
    monkeypatch.setattr(shim_reporter, 'RUN_RESULTS_PATH', tmp_path / 'x.json')
    assert pkg_reporter.run_results_path() == tmp_path / 'x.json'


# --- 4. this repo's injected policy -----------------------------------------

def test_the_injected_policy_is_this_repo_s_permissive_one(tmp_path):
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
        },
    }), encoding='utf-8')

    lookup, counts = policy(mapping_file)
    assert lookup == {'tests/a.py::test_one': 'K1', 'tests/a.py::test_two': 'K2'}, (
        'this repo has no review gate: every mapped row must upload')
    assert counts['total'] == 2
    assert counts['unreviewed'] == 0, (
        'a non-zero unreviewed count would print a warning saying these rows '
        'do not upload, which is false under the permissive policy')
