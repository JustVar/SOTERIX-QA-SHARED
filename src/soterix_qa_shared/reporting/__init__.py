"""Reporting leaves shared by the QA repos.

Standard library only, and nothing here knows about QA Touch or pytest:

  * `paths`    fitting a file name under Windows' 260-character path limit
  * `products` QA Touch project keys and the product each one names
  * `branding` the company logo, embedded into a generated page

Two of the three need something from the consumer before they work, because
neither a repo root nor a directory layout is the package's to guess:

    from soterix_qa_shared.reporting import branding, products

    branding.configure(REPO_ROOT)
    products.configure_path_rules([('tests/ui/c2c/', '60W5')])

`paths` needs nothing. It takes a directory and a stem and returns a name.

The rest of the reporting stack (the archive, the run and health pages, the
media capture, the mailer) stays in the UI repo until a second consumer
needs it. It is 4,338 lines and wants its own design pass, not a move.
"""
from .branding import COMPANY, favicon_link, header_logo
from .paths import PATH_MAX, digest, fit_name, long_root_warning
from .products import (
    DEFAULT_KEY,
    MIXED_PREFIX,
    PRODUCTS,
    is_mixed,
    product_for,
    product_name,
    run_product,
    target_product,
)

__all__ = [
    "COMPANY",
    "DEFAULT_KEY",
    "MIXED_PREFIX",
    "PATH_MAX",
    "PRODUCTS",
    "digest",
    "favicon_link",
    "fit_name",
    "header_logo",
    "is_mixed",
    "long_root_warning",
    "product_for",
    "product_name",
    "run_product",
    "target_product",
]
