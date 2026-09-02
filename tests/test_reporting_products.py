"""Unit tests for soterix_qa_shared.reporting.products.

The project table, the name lookup, the precedence of `product_for`
(marker, path rule, mapping default, environment, NexaiQ-Core), and the run
label for one product or a mix.

The doc-sync test (this table vs docs/QA_TOUCH_INTEGRATION.md) lives in
the consumer: the doc is the source of truth there, and it reaches this
table through the shim.
"""
from __future__ import annotations

import pytest

from soterix_qa_shared.reporting import products

CORE = 'tests/ui/views/test_views_basic.py::TestViews::test_open[chromium]'
C2C = 'tests/ui/c2c/test_c2c_camera.py::TestCamera::test_reboot[chromium]'

# The UI repo's rules. The package ships none; a consumer supplies these.
UI_RULES = (('tests/ui/c2c/', '60W5'),)


@pytest.fixture
def ui_rules():
    """Install the UI repo's path rules for one test, then restore."""
    before = products.PATH_RULES
    products.configure_path_rules(UI_RULES)
    yield
    products.PATH_RULES = before


def test_the_package_ships_no_path_rules():
    """A rule names one repo's folders. Shipping one would route another
    repo's tests to a product whose run key the session does not have, and
    the failure would be a skipped upload nobody asked for."""
    assert products.PATH_RULES == ()


def test_table_carries_the_six_projects():
    """The consumer repo owns the doc this must match; see the module
    docstring. Pinned here so a careless edit still fails somewhere."""
    assert products.PRODUCTS == {
        'JlW1': 'NexaiQ-Core', 'P0BK': 'NexaiQ-Neurosen',
        '60w8': 'NexaiQ-Technician App', '70l1': 'NexaiQ-Partner Admin Tool',
        'K5kB': 'NexiaQ-Mobile', '60W5': 'NexaiQ-Camera 2 Cloud'}


def test_product_name_known_unknown_and_empty():
    """A known key names its product. An unknown key stays visible as itself."""
    assert products.product_name('JlW1') == 'NexaiQ-Core'
    assert products.product_name('60W5') == 'NexaiQ-Camera 2 Cloud'
    assert products.product_name('ZZZ9') == 'ZZZ9'
    assert products.product_name('') == ''
    assert products.product_name(None) == ''


# ---------------------------------------------------------------------------
# configure_path_rules
# ---------------------------------------------------------------------------

def test_configure_path_rules_takes_the_consumers_rules(ui_rules):
    assert products.PATH_RULES == UI_RULES
    assert products.product_for(C2C) == '60W5'


def test_configure_path_rules_normalises_to_a_tuple_of_pairs():
    """A list of lists from JSON must behave like the literal tuple."""
    before = products.PATH_RULES
    try:
        products.configure_path_rules([['tests/api/x/', 'P0BK']])
        assert products.PATH_RULES == (('tests/api/x/', 'P0BK'),)
        assert products.product_for('tests/api/x/test_a.py::test_b') == 'P0BK'
    finally:
        products.PATH_RULES = before


def test_configure_path_rules_replaces_rather_than_appends():
    """Last call wins. Two shims in one process must not stack rules."""
    before = products.PATH_RULES
    try:
        products.configure_path_rules([('a/', 'K1')])
        products.configure_path_rules([('b/', 'K2')])
        assert products.PATH_RULES == (('b/', 'K2'),)
    finally:
        products.PATH_RULES = before


def test_with_no_rules_a_c2c_path_falls_through_to_the_default(monkeypatch):
    """The package's own behaviour, and the API repo's: no folder rules, so
    the path never decides the product."""
    monkeypatch.delenv(products.ENV_VAR, raising=False)
    assert products.PATH_RULES == ()
    assert products.product_for(C2C) == 'JlW1'
    assert products.product_for(C2C, None, 'K5kB') == 'K5kB'


# ---------------------------------------------------------------------------
# Precedence
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(('nodeid', 'marker', 'default', 'env', 'key'), [
    (C2C, 'P0BK', 'K5kB', '70l1', 'P0BK'),     # the marker beats everything
    (C2C, None, 'K5kB', '70l1', '60W5'),       # the path rule beats the mapping default
    ('tests\\ui\\c2c\\test_x.py::test_a', None, None, None, '60W5'),  # backslashes too
    ('tests/ui/c2c', None, 'K5kB', None, '60W5'),   # the folder itself, a run target
    ('tests/ui/c2c/', None, None, None, '60W5'),
    (CORE, None, 'K5kB', '70l1', 'K5kB'),      # the mapping default beats the environment
    (CORE, None, None, '70l1', '70l1'),        # the environment beats the built-in default
    (CORE, None, None, '  ', 'JlW1'),          # a blank environment value is no value
    (CORE, None, None, None, 'JlW1'),          # nothing set: NexaiQ-Core
    ('tests/ui/c2c_extra/test_x.py::test_a', None, None, None, 'JlW1'),  # the rule wants the folder
    ('', None, None, None, 'JlW1'),
])
def test_product_for_precedence(monkeypatch, ui_rules, nodeid, marker, default, env, key):
    """Marker, then the c2c path rule, then the mapping default, then the
    environment, then NexaiQ-Core."""
    if env is None:
        monkeypatch.delenv(products.ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(products.ENV_VAR, env)
    assert products.product_for(nodeid, marker, default) == key


# ---------------------------------------------------------------------------
# Run labels
# ---------------------------------------------------------------------------

def test_run_product_single_mixed_and_none():
    """One name when the tests agree, a sorted "Mixed:" label otherwise."""
    assert products.run_product(['NexaiQ-Core', 'NexaiQ-Core']) == 'NexaiQ-Core'
    mixed = products.run_product(['NexaiQ-Core', 'NexaiQ-Camera 2 Cloud', '', 'NexaiQ-Core'])
    assert mixed == 'Mixed: NexaiQ-Camera 2 Cloud, NexaiQ-Core'
    assert products.is_mixed(mixed)
    assert not products.is_mixed('NexaiQ-Core')
    assert not products.is_mixed('')
    assert products.run_product([]) == ''
    assert products.run_product(['', None]) == ''


def test_target_product_names_a_run_from_its_targets(monkeypatch, ui_rules):
    """A run that recorded no product is named by its targets: the c2c
    folder or node id is Camera 2 Cloud, anything else the default, two
    kinds a sorted mix, and no target the default. Never ''."""
    monkeypatch.delenv(products.ENV_VAR, raising=False)
    assert products.target_product(['tests/ui/c2c']) == 'NexaiQ-Camera 2 Cloud'
    assert products.target_product([C2C]) == 'NexaiQ-Camera 2 Cloud'
    assert products.target_product(['tests/ui/views']) == 'NexaiQ-Core'
    assert products.target_product(['tests/ui/views', 'tests/ui/c2c']) == \
        'Mixed: NexaiQ-Camera 2 Cloud, NexaiQ-Core'
    assert products.target_product([]) == 'NexaiQ-Core'
    assert products.target_product(['', '  ']) == 'NexaiQ-Core'
    assert products.target_product([], 'K5kB') == 'NexiaQ-Mobile'
    assert products.target_product(['tests/ui/c2c'], 'K5kB') == 'NexaiQ-Camera 2 Cloud'
    monkeypatch.setenv(products.ENV_VAR, '70l1')
    assert products.target_product(['tests/ui/views']) == 'NexaiQ-Partner Admin Tool'
