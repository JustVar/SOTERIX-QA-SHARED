"""QA Touch project keys and the product each one stands for.

The table is the shared one: every Soterix QA repo reports into the same six
QA Touch projects, so the mapping from key to product name belongs here
rather than in each repo.

`PATH_RULES` does not. A rule like "tests under tests/ui/c2c/ belong to
Camera 2 Cloud" is true of one repo's directory layout and meaningless in
another, so the consumer sets it and the package ships an empty tuple. A
consumer that sets nothing simply has no path rules, which is the correct
behaviour for a repo whose tests are not split by product folder.

`product_for` names a test's project key, first hit wins:

  1. an explicit `qatouch_project` marker on the test;
  2. a path rule the consumer supplied;
  3. the mapping file's top-level project_key (the caller reads it);
  4. the QATOUCH_PROJECT_ID environment variable;
  5. NexaiQ-Core (JlW1), the project every other tool defaults to.

`target_product` names a run's product from its invocation targets alone
(steps 2 to 5, there is no marker to read), for archives that recorded no
per-test product.

Standard library only.
"""
from __future__ import annotations

import os
from collections.abc import Iterable

PRODUCTS = {
    'JlW1': 'NexaiQ-Core',
    'P0BK': 'NexaiQ-Neurosen',
    '60w8': 'NexaiQ-Technician App',
    '70l1': 'NexaiQ-Partner Admin Tool',
    'K5kB': 'NexiaQ-Mobile',
    '60W5': 'NexaiQ-Camera 2 Cloud',
}

DEFAULT_KEY = 'JlW1'
ENV_VAR = 'QATOUCH_PROJECT_ID'

# (path prefix under the repo, project key). The prefix matches the node
# id's file part with forward slashes. First match wins.
#
# Consumer-supplied: call configure_path_rules() from the shim. Empty here
# on purpose. A package-level default would be one repo's layout imposed on
# every other, and the failure would be silent: a test routed to a product
# whose run key the session does not have.
PATH_RULES: tuple[tuple[str, str], ...] = ()

MIXED_PREFIX = 'Mixed: '


def configure_path_rules(rules: Iterable[tuple[str, str]]) -> None:
    """Set the consumer's path rules. Called by the shim, last call wins."""
    global PATH_RULES
    PATH_RULES = tuple((str(prefix), str(key)) for prefix, key in rules)


def product_name(key: str | None) -> str:
    """The product a project key stands for.

    An unknown key comes back as itself, so a page never hides a key it
    cannot name. None or an empty key is ''.
    """
    if not key:
        return ''
    return PRODUCTS.get(str(key), str(key))


def product_for(nodeid: str, marker_project: str | None = None,
                default_key: str | None = None) -> str:
    """The project key of a test, by the precedence in the module docstring.

    `marker_project` is the qatouch_project marker's value when the test
    carries one. `default_key` is the mapping file's top-level project_key
    when the caller could read it.
    """
    if marker_project:
        return str(marker_project)
    path = str(nodeid or '').split('::', 1)[0].replace('\\', '/').lstrip('/')
    # The rule's prefix ends in '/'. The folder itself ('tests/ui/c2c', a
    # run target) must match too, so the path is compared as a folder.
    for prefix, key in PATH_RULES:
        if f"{path.rstrip('/')}/".startswith(prefix):
            return key
    if default_key:
        return str(default_key)
    return os.environ.get(ENV_VAR, '').strip() or DEFAULT_KEY


def target_product(targets: Iterable[str], default_key: str | None = None) -> str:
    """The product a run's invocation targets name, as `run_product` labels
    it: one name, or "Mixed: A, B" when the targets differ. No target
    (the whole suite) is the default. Never '': a page that shows this has
    no blank to fall back to."""
    names = [product_name(product_for(t, None, default_key))
             for t in targets if str(t or '').strip()]
    return run_product(names) or product_name(product_for('', None, default_key))


def run_product(names: Iterable[str]) -> str:
    """One label for a run from its tests' product names.

    The single name when every test agrees, else "Mixed: A, B" with the
    names sorted, so two runs of one mix read the same. '' for no names.
    """
    distinct = sorted({str(n) for n in names if n})
    if not distinct:
        return ''
    if len(distinct) == 1:
        return distinct[0]
    return MIXED_PREFIX + ', '.join(distinct)


def is_mixed(product: str | None) -> bool:
    return str(product or '').startswith(MIXED_PREFIX)
