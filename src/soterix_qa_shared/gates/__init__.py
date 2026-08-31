"""CI gates shared by the QA repositories.

A gate here is a check a repository runs in CI and can also run locally. It
takes the repository's root and its own configuration as arguments rather
than reading them from the environment, so the same code serves repos whose
suites are laid out differently.
"""
