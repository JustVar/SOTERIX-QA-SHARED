# Status

The package holds the code the two Soterix test repositories share: the
QA Touch client and reporter, the run archive and its pages, the health
page, the Jira layer and the collect-floor gate. `README.md` lists every
module.

- Both test repositories, Soterix-WebApp-QA and Soterix-API-QA, install
  the package as a built wheel from their own `vendor/` folder. Neither
  needs access to this repository to run.
- A change here reaches them only as a new wheel. The release steps are in
  `README.md` under Install, and in `docs/MERGE_RUNBOOK.md`.
- CI runs on GitHub's machines on every push: the tests, `ruff`, the prose
  check, a built-wheel import check, and on a `v*` tag the check that the
  tag equals the version.
- The package reads no tenant or machine variable. One QA server is the
  assumption. A run's provenance still records the machine, the operator
  and the account, so runs from different machines read apart.
- Rules that must stay true are in `docs/RULES.md`. The trap behind each
  coding convention is in `docs/CONVENTIONS.md`.
