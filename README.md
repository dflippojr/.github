# .github

Default community health files for repositories owned by dflippojr. GitHub applies them to any repository that doesn't have its own. A repository's own `.github` files take precedence over these, file by file.

## Contents

- `.github/ISSUE_TEMPLATE/feature.md`: template for features and changes (Outcome, Background, Scope, Decisions, Open questions, Dependencies, Acceptance criteria).
- `.github/ISSUE_TEMPLATE/bug.md`: template for bug reports.
- `.github/ISSUE_TEMPLATE/verification.md`: template for confirming something already built works.
- `.github/ISSUE_TEMPLATE/config.yml`: new-issue chooser settings.
- `PULL_REQUEST_TEMPLATE.md`: default pull request description.
- `CONTRIBUTING.md`: how issues, labels, branches and pull requests work, including how to write an issue.
- `labels.json`: the shared label set.
- `scripts/sync_labels.py`: shows or applies label differences across repositories. See `CONTRIBUTING.md`.
- `tests/`: tests for the label configuration and script.

## CI and analysis

Install the CI dependencies and run the full test suite:

```sh
python -m pip install -r requirements-dev.txt
python -m pytest -q --cov=scripts --cov-report=xml:coverage.xml
```

Dependabot checks GitHub Actions and the root `requirements-dev.txt` weekly.
CI runs tests on every pull request and pushes to `main`. The SonarCloud
workflow also generates coverage and analyses `scripts/sync_labels.py`, with
`tests/` classified as tests, on `main` pushes and same-repository pull requests,
including Dependabot. Fork pull requests run CI tests without SonarCloud access.
When `SONAR_TOKEN` is absent, the workflow emits a notice and skips the scan;
tests still run. A notice-only run does not verify SonarCloud analysis.

The owner must confirm or create the SonarCloud project `dflippojr_.github` in
organization `dflippojr`, linked to this repository, restore scanner access if
needed, and disable automatic analysis when enabling the CI scanner. Add an
authorized analysis token under the exact secret name `SONAR_TOKEN` to both
repository Actions secrets and repository Dependabot secrets. Dependabot PRs
cannot use the Actions copy of the secret.

After setup, verify analysis on a `main` push, an ordinary same-repository PR,
and a real Dependabot PR. Record the workflow runs and SonarCloud results in
issue #8, together with the verified Dependabot configuration. The Dependabot
run must publish analysis for its PR/head commit rather than only a missing-token
notice.
