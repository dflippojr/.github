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
- `.github/workflows/gate-guard.yml` and `scripts/gate_guard.py`: the reusable gate guard. See below.
- `tests/`: tests for the label configuration and the scripts. `tests/fixtures/gate_guard/` holds the gate guard's fixture diffs.

## Gate guard

A reusable workflow that makes "don't weaken the gate" a check. It reads what
merging the pull request would change and reports each hit in the job summary:

| Rule | Hits |
| --- | --- |
| `removed-tests` | A deleted test file, or fewer test functions in a file (`def test_`, `test(`/`it(`, `@Test`, `#[test]`, `func Test`). |
| `skip-marker` | A new `pytest.mark.skip`/`skipif`/`xfail`, `unittest.skip`, `.skip(`/`.only(`/`test.fixme`/`xit(`/`xdescribe(`, `@Disabled`, `#[ignore]`, or pytest `--deselect`/`-k "not …"` in config. `pytest.skip()` and Go `t.Skip()` count when unconditional or when the condition checks `CI`. |
| `retry` | New or raised Playwright `retries`, `--reruns`, `pytest-rerunfailures`, `flaky`, `jest.retryTimes`, Surefire `rerunFailingTestsCount`. |
| `gate-config` | Any edit to `.github/workflows/**`, `sonar-project.properties`, `CODEOWNERS` or coverage config (`.coveragerc`, `[tool.coverage]`, `fail_under`, JaCoCo rules, c8/nyc/Jest thresholds); a lint rule disabled; a size or performance budget raised. |

A test renamed in place or moved to another file, a renamed test file, and a
skip marker that moves with its test are not hits. The job fails on any hit
unless the pull request carries the `gate-change-approved` label; with the label
it passes and still lists every hit. Only the owner applies that label: it
records that the owner reviewed the removals or gate changes on this PR.

The workflow needs no secrets. It reads labels with the caller's `GITHUB_TOKEN`,
runs the script with the runner's `python3` (3.7 or later, standard library
only), and uses no action except `actions/checkout`, pinned by SHA.

Call it from a workflow in each repository:

```yaml
name: Gate guard

on:
  pull_request:
    # labeled/unlabeled re-run the check when the approval label changes.
    types: [opened, synchronize, reopened, labeled, unlabeled]

permissions:
  contents: read
  issues: read
  pull-requests: read

jobs:
  gate-guard:
    uses: dflippojr/.github/.github/workflows/gate-guard.yml@main
    # Private repositories run on the tower:
    # with:
    #   runs-on: '["self-hosted", "Linux", "X64", "tower-linux"]'
```

| Input | Default | Meaning |
| --- | --- | --- |
| `runs-on` | `'"ubuntu-latest"'` | Runner as JSON: a label string or a list of labels. |
| `require-owner-label-actor` | `false` | Accept the label only when the repository owner applied it, checked through the issue-events API. Turn it on once workers act under their own identity (dflippojr/agent-ops#44). |
| `guard-ref` | `main` | Ref of this repository to load `scripts/gate_guard.py` from. If you pin `uses:` to a SHA or tag, set `guard-ref` to the same value. |

The check appears as `gate-guard / gate-guard`; make it a required status check
to enforce it. On events other than `pull_request` the job only prints a notice.

Run it locally against a branch or a saved diff:

```sh
python3 scripts/gate_guard.py --repo-dir ../some-repo --base origin/main --head HEAD
gh pr diff 80 -R dflippojr/disc-golf-league > pr.diff && python3 scripts/gate_guard.py --diff-file pr.diff
```

`gate-guard-self-test.yml` runs the rule tests and checks every fixture's
verdict on each pull request here: `flag-*.diff` fixtures must fail and
`pass-*.diff` fixtures must pass.

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
