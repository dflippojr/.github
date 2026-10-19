# .github

Default community health files for repositories owned by dflippojr. GitHub applies them to any repository that doesn't have its own. A repository's own `.github` files take precedence over these, file by file.

## Contents

- `.github/ISSUE_TEMPLATE/feature.yml`: issue form for features and changes (Outcome, Background, Scope, Decisions, Open questions, Dependencies, and a required Acceptance criteria checklist).
- `.github/ISSUE_TEMPLATE/bug.yml`: issue form for bug reports, with required *Introduced by* (PR, commit or "unknown") and *Gate that should have caught it* fields.
- `.github/ISSUE_TEMPLATE/verification.md`: template for confirming something already built works.
- `.github/ISSUE_TEMPLATE/config.yml`: new-issue chooser settings.
- `PULL_REQUEST_TEMPLATE.md`: default pull request description, with a Proof table (acceptance criterion, how verified, evidence), Not verified and Gate changes.
- `CONTRIBUTING.md`: how issues, labels, branches and pull requests work, including how to write an issue.
- `labels.json`: the shared label set.
- `scripts/sync_labels.py`: shows or applies label differences across repositories. See `CONTRIBUTING.md`.
- `.github/workflows/gate-guard.yml` and `scripts/gate_guard.py`: the reusable gate guard. See below.
- `.github/workflows/gate-guard-target.yml`, `templates/gate-guard-target-caller.yml` and `.github/workflows/gate-guard-base.yml`: the gate guard on the default branch, its caller template, and this repository's own caller. See "Gate guard on the default branch".
- `tests/`: tests for the label configuration, the issue forms and PR template, and the scripts. `tests/fixtures/gate_guard/` holds the gate guard's fixture diffs and `tests/fixtures/gate_guard_target/` its owner-label scenarios.

## Secret scan

The reusable **secret-scan** workflow, hashed private identifier configuration,
financial fixture scan and owner push-protection setup are documented in
[docs/secret-scan.md](docs/secret-scan.md). Use
`templates/secret-scan-caller.yml` when wiring a repository.

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

## Gate guard on the default branch

`gate-guard.yml` runs on `pull_request`, which takes its workflow files from
the pull request's head. A pull request can edit its own caller to drop the
owner-label rule or the guard, and the check still goes green.
`gate-guard-target.yml` closes that hole: it runs on `pull_request_target`,
which always takes the workflow from the repository's default branch, whatever
the pull request's base (GitHub's rule since 2025-12-08). It applies the same
rules. A caller therefore takes effect only once it is on the default branch.
Keep `gate-guard.yml` working until every caller has moved over.

What it does on each `opened`, `synchronize`, `reopened`, `edited`, `labeled`,
`unlabeled` and `closed` event. `edited` covers a pull request moved to another
base: the same head against a new base can bring hits the old base hid.
`closed` re-judges the pull requests still open with the head.

1. Loads `scripts/gate_guard.py` from this repository at the full commit SHA in
   `guard-ref`. Anything other than 40 hex characters fails the job.
2. Lists every open pull request whose head is the event's head commit, then
   reads each candidate and the event's own PR individually for its current
   state, head and base. It judges only those still open at that head, as in
   steps 3 and 4; an unreadable current state fails closed. A
   check run belongs to a commit, not to a pull request, so the guard makes one
   decision per commit: two pull requests that share a head but not a base can't
   let the passing one's result replace the failing one's. The caller's
   concurrency group is keyed by the head SHA, so the newest decision for a
   commit is the one that lands. A currently closed pull request is left out,
   so closing the one that fails lets the others pass. Rerunning an older
   `closed` event after reopening judges the reopened PR too: the event's
   payload never decides its current state, head or base. When none
   is left open with the head, the check completes as `neutral` with no
   verdict, and both label outputs are `false`.
3. Asks the compare API for each pull request's merge base, then fetches the
   merge base and the head commit as git objects into an empty repository
   (`git fetch --depth=1`, no checkout) and reads their diff, with no external
   diff or textconv driver.
4. Checks the owner-only labels, `gate-change-approved` and
   `findings-accepted`. One counts only when all of these are true:
   - the newest labeled/unlabeled event for it is `labeled` by the owner (the
     same rule as `ao owner-label`), so a label a worker applies or re-applies
     doesn't count;
   - it was applied after the head commit was pushed. The push time comes from
     GitHub's repository activity record, not from commit dates, which whoever
     makes the commit sets. A push makes the owner's label stale. If GitHub has
     no record of the push, the label doesn't count.
   - the whole label history was read. A list longer than 30 pages of 100
     fails the job rather than judge from its oldest part.
5. Posts the verdict as a check run named **`gate-guard`** on the head commit,
   with the hit table and the label verdicts of each pull request. It fails
   when any of them has unapproved hits, as `gate-guard.yml` does. It also
   fails while `findings-accepted` is on a pull request but doesn't count, so
   nothing can lean on a label the owner didn't apply to this head. A job that
   can't read a diff, the labels or the list of open pull requests posts a
   failure.

   Before judging anything, the job creates the check run as `in_progress`,
   which replaces any earlier `gate-guard` result on the commit, and fills in
   the verdict at the end. A job that dies or is cancelled in between leaves a
   blocking check, never an earlier success. The report is cut to fit the
   Checks API's limit, which counts UTF-8 bytes; if GitHub still rejects it,
   the check fails with a short note and the job summary keeps the report.

Require `gate-guard` in the ruleset. The workflow job's own check also shows
on the pull request, but its name (`judge / judge` with the template) comes
from the caller's job names, and it carries no report. The job also exposes
`conclusion`, `gate-change-approved` and `findings-accepted` as outputs; a
label output is `true` only when the label counts on every open pull request
with the head.

Copy `templates/gate-guard-target-caller.yml` to
`.github/workflows/gate-guard-target.yml` in each repository and put the same
full SHA of this repository's `main` in both places it asks for one. This
repository calls the local copy through `gate-guard-base.yml`, with
`guard-ref: ${{ github.sha }}`, which under `pull_request_target` is the
default branch's newest commit.

| Input | Default | Meaning |
| --- | --- | --- |
| `guard-ref` | required | Full commit SHA of this repository to load the script from; the SHA in the caller's `uses:` line. |
| `runs-on` | `'"ubuntu-latest"'` | Runner as JSON. Private repositories pass the tower's labels. |
| `owner` | repository owner | Login whose labels count. |

Every edit to `.github/workflows/**` is a `gate-config` hit, so a pull request
that edits its caller or `gate-guard-target.yml` itself needs the owner's
`gate-change-approved` label, applied after its newest push.

### Why `pull_request_target` is safe here

`pull_request_target` is risky because the job runs with a write-capable token
in the base repository's context. This workflow stays safe by never running
anything from the pull request:

- **No pull request code runs.** No step checks out the pull request. The
  script fetches its commits as objects only. Nothing is written to a working
  tree, so no hook, build file, `.gitattributes` driver, submodule or LFS
  object from the pull request takes effect. The only checkout is this
  repository's script, at a pinned SHA.
- **The judge comes from the default branch.** The caller, the reusable
  workflow (local, or pinned by SHA) and the script (pinned by SHA) are all
  fixed by the default branch, even for a pull request into another branch.
  Every action is pinned by full SHA.
- **No secrets.** Only the default `GITHUB_TOKEN`, with `contents: read`,
  `issues: read`, `pull-requests: read` and `checks: write`. `checks: write` is
  there only to post the `gate-guard` check run on the head commit. The token reaches git
  through the environment, never a command line or `.git/config`.
- **No untrusted input in shell.** No `run:` step interpolates pull request
  fields such as the branch name or title. The script reads the event payload
  as JSON.

### Threats it still doesn't cover

- **A spoofed `gate-guard` check.** Any workflow with `checks: write` can post
  a check run named `gate-guard`, and so can a `pull_request` job named
  `gate-guard`. Both come from the GitHub Actions app, as the real one does, so
  `check_integration_id` can't tell them apart. A same-repository pull request
  that adds such a workflow is itself a `gate-config` hit, and the real guard
  fails it, but if the spoof finishes later it may be the result a ruleset
  reads. The ruleset feature that pins a required workflow ("Require workflows
  to pass") is for organizations only. Until then, the owner reads every pull
  request that edits a workflow before merging it. Weigh this before giving the
  Workflows permission back to `dflippojr-workers`.
- **The owner's identity.** A label applied by the owner login counts, whoever
  was driving it. Owner-identity launches (`AO_WORKER_IDENTITY=owner`) and a
  leaked owner token are outside what this check can see.
- **What a diff can't show.** A test made vacuous (`assert True`), a slower
  test, or a weaker assertion is not a hit. The guard looks for removals,
  skips, retries and config edits, not for meaning.
- **Direct pushes to the default branch.** They never trigger this workflow. The
  default-branch ruleset (agent-ops `config/rulesets.yaml`) blocks them.
- **`gate-guard.yml` callers.** Repositories still on the `pull_request`
  version keep the old hole until they move.

## Workflow lint

`.github/workflows/workflow-lint.yml` is a reusable `workflow_call` check named
`workflow-lint`. Copy `templates/workflow-lint-caller.yml` into a consumer and
replace both placeholders with the same full SHA of this repository after merge.
It uses plain `pull_request`, no secrets and only `contents: read` (GitHub's
repository metadata access is implicit). Its job currently uses `ubuntu-latest`,
including for private callers; that deliberate hosted job is visible as a private
runner warning when auditing this reusable workflow. No consumer is migrated by
this change. The check appears as `workflow-lint / workflow-lint` with the template.

The script reads visibility from the repository API. It fails public repositories'
`self-hosted` labels, including literal JSON expressions, matrix entries and
reusable-call runner inputs. `config/workflow-lint.json` lists the only exceptions:
the `review` jobs in the two harness repositories' `review.yml`, with their exact
fork guards, expiring at 2027-04-12 UTC. A moved job, removed guard or expired
entry fails. Private GitHub-hosted labels produce warnings; the
`${{ fromJSON(vars.CI_RUNS_ON || '...') }}` form is allowed in private repositories.
Unresolvable runner expressions produce warnings for manual review; static
analysis cannot determine arbitrary runtime variables or called workflow code.

All current workflows (`.yml` and `.yaml`), tracked `ci-local*` scripts at any
depth, and `package.json` scripts are checked for pytest `-n`/`--numprocesses`
and Playwright `--workers`. Literal counts above four, pytest `auto`/`logical`,
and percentage counts fail. Two and four are allowed. Dynamic counts warn;
the check does not interpret arbitrary shell logic, wrappers or computed flags.
Options that pytest reads before the command line count too: a literal
`PYTEST_ADDOPTS` in workflow, job or step `env`, or assigned in a `run` step, a
package script, a `ci-local` shell or Python/JS launcher (including `export`,
`env`, `$env:`, `set` and `$GITHUB_ENV`), and `addopts` in `pytest.ini`,
`.pytest.ini`, `tox.ini`, `setup.cfg`, `pyproject.toml` and `pytest.toml`. A worker
flag on the same pytest command line replaces the environment value, so it is
judged instead. A value that depends on an expression or variable warns.
A `PYTEST_ADDOPTS` value written to `$GITHUB_ENV` is checked in the step that
writes it, but such a write never clears or weakens the value later steps
inherit from workflow or job `env`: whether it lands as its own line depends on
the shell, stream, line breaks and later overwrites, which the YAML does not
show. To change the inherited value for a step, set `PYTEST_ADDOPTS` in that
step's own `env:` block.
Only `run` steps and package scripts are commands; comments and dependencies
are not. This checks the whole head tree so an unchanged unsafe setting remains
visible when another file changes.

Actionlint checks only added or modified PR workflows, relative to the API's
merge base; deleted workflows and PRs with no workflow changes do not invoke it.
Actionlint's source is pinned to a full commit SHA, as are all actions. A fresh
temporary directory and trusted `config/actionlint.yaml` prevent PR config from
suppressing diagnostics. The directory holds an empty `.git` marker and every head
workflow, so local reusable calls resolve and a missing callee, a missing required
input or an undefined input is reported; only the changed workflows are linted. Custom runner names are accepted by actionlint and
judged by our visibility rules. ShellCheck and Pyflakes subprocesses are disabled.
Target files are read from a shallow bare git repository, without checkout,
hooks, submodules, symlink traversal, dependency installation or target execution.

Local policy tests: `python -m pytest -q tests/test_workflow_lint.py`. With the
pinned actionlint executable on PATH and `GITHUB_TOKEN` set, run:

```sh
python scripts/workflow_lint.py --repository dflippojr/some-repo --output report.json
python scripts/workflow_lint.py --repository dflippojr/some-repo --event-file event.json
python scripts/audit_workflow_lint.py --rulesets rulesets.yaml --rulesets-revision FULL_SHA --output audit.json
```

Without an event file, every current default-branch workflow is sent to actionlint.
The audit includes all `repos` entries, even disabled/held ones, records each head
and visibility, and reports fetch errors explicitly. Violations do not abort the
inventory. See `docs/workflow-lint-audit.md` for the initial audit and limitations.

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
