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

What it does on each `opened`, `synchronize`, `reopened`, `edited`, `labeled`
and `unlabeled` event. `edited` covers a pull request moved to another base:
the same head against a new base can bring hits the old base hid.

1. Loads `scripts/gate_guard.py` from this repository at the full commit SHA in
   `guard-ref`. Anything other than 40 hex characters fails the job.
2. Lists every open pull request whose head is the event's head commit, with
   each one's current base, and judges each of them as in steps 3 and 4. A
   check run belongs to a commit, not to a pull request, so the guard makes one
   decision per commit: two pull requests that share a head but not a base can't
   let the passing one's result replace the failing one's. The caller's
   concurrency group is keyed by the head SHA, so the newest decision for a
   commit is the one that lands. Closing the pull request that fails doesn't
   re-judge the others; a push, an edit or a label change on one of them does.
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

## PR body lint

`.github/workflows/pr-body-lint.yml` supplies an Actions job named **pr-body**.
Copy `templates/pr-body-lint-caller.yml` to the calling repository's default
branch and replace both `<full-sha>` placeholders with the same full commit
SHA of this repository's `main`. Require the resulting `pr-body / pr-body`
job in the caller's ruleset. Wiring individual repositories is separate work.

The caller uses `pull_request_target` on opened, edited, synchronize,
reopened and labeled events, plus unlabeled and assignee changes. The lint
fetches the current body, labels, assignees, linked issues, changed filenames
and head's gate-guard report through the GitHub API. It requires:

- `## Proof` (also accepts `## Proof table`) containing a Markdown table with
  at least one non-empty data row; a blank template row does not count.
- `### Not verified` and `Closes #N`, `Fixes #N` or `Resolves #N` (case
  insensitive). Cross-repository references such as `Fixes owner/repo#12`
  also work; the token must be able to read the linked issue.
- Assignee `dflippojr`.
- A non-empty `### Gate changes` explanation other than `None` when the
  latest head check named `gate-guard` reports findings, including when that
  check passed with owner approval. The lint waits up to three minutes for the
  guard's check to appear and complete, because both start on the same event.
  A check still missing or pending after that, or an unreadable report, fails
  closed with a rerun instruction; it never counts as passing.
  **`gate-guard-target.yml` is required**: only its check run is named exactly
  `gate-guard`. The `pull_request` flavour (`gate-guard.yml`) reports
  `gate-guard / gate-guard` and writes no check output, and a repository with no
  guard has no such check, so in both cases `pr-body` stays red until
  `gate-guard-target.yml` is installed. Require the guard independently; this
  check does not approve gate edits.
- A changed test path when any linked issue has label `bug`. A `bug` label on
  the PR itself does not substitute for the linked issue's label. Renames count
  both old and new filenames, and deleted tests count as touched paths; the
  gate guard separately checks removals.

Comments and fenced examples do not satisfy these rules. Dependabot's exact
login `dependabot[bot]` is exempt from the Proof section (including Not
verified) and closing-reference requirements. Assignee, gate explanations,
linked-bug test paths and custom proof requirements still apply.

### Per-repository proof rules

An optional `.github/pr-proof-rules.yml` is read from the caller's **default
branch**, never the PR head or its chosen base. It contains `test_paths`
(a replacement list of test globs) and `rules` (a mapping from changed-file
globs to lists of required body regexes). Every regex for every matched glob
must match visible body text. Regexes use Python syntax with case-insensitive
and multiline matching. Invalid rules and incomplete API results fail closed.

The default test globs are `tests/**`, `test/**`, `**/*_test.*`,
`**/*.test.*` and `**/test_*`. Globs match repository-relative paths using
Python `fnmatch`; `*` can include path separators, and a leading `**/` also
matches files at the repository root.

Examples for the four repositories in issue #20 (adjust the file globs to
the repository's actual paths before adopting):

```yaml
# disc-golf-league: give changed money/standings logic a named invariant.
rules:
  '**/money/**': ['Invariant: .+']
  '**/*standings*': ['Invariant: .+']
```

```yaml
# fhir-crd-router: validator changes cite the specification.
rules:
  '**/*validator*': ['Spec: https://[^\s]+']
```

```yaml
# open-payer-sandbox: list changed goldens in the body.
rules:
  '**/goldens/**': ['Changed goldens: .+']
```

```yaml
# task-start-copilot: command changes carry an eval summary.
test_paths: ['tests/**', 'evals/**']
rules:
  'commands/**': ['Eval summary: .+']
```

### Security and verification

Like the default-branch gate guard, the caller and reusable workflow must be
trusted default-branch definitions pinned to the same full script SHA.
The only checkout is this repository's lint script at that pin, with persisted
credentials disabled. It never checks out or runs PR code, reads PR dependency
files, uses PR rules, or interpolates PR fields into shell commands. PyYAML
is installed at a fixed version; rules use `safe_load`. No secrets are needed
beyond the default `GITHUB_TOKEN`, restricted to `contents: read`,
`issues: read`, `pull-requests: read`, and `checks: read`. The last permission
reads gate-guard checks in private repositories; no check-write permission is
needed because Actions reports the job result. A fork cannot supply its own
judge. As with gate guard, a compromised owner identity or a separate workflow
spoofing a check name remains outside this protection.

`lint-ref` is required; `runs-on` defaults to `'"ubuntu-latest"'` and accepts
the same JSON runner-label input as gate guard. Private callers can select
`'["self-hosted", "Linux", "X64", "tower-linux"]'`.

Run the fixtures and API/security tests locally with
`python -m pytest -q tests/test_pr_body_lint.py`. For a live, read-only check
of a scratch PR, set `GITHUB_TOKEN` to a token with those read permissions and
run `python scripts/pr_body_lint.py --repo owner/repo --pr N`. Exit codes are
0 (pass), 1 (body violations), and 2 (could not verify). Short error messages
go to stdout and, in Actions, the step summary. Test both an incomplete and a
complete scratch body. The reusable `pull_request_target` caller itself takes
effect only after its trusted definition is on the default branch.

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
