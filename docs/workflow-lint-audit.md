# Workflow lint audit — 2026-10-19

Read-only audit of all 17 `repos` entries (including disabled/held entries) in
[`agent-ops/config/rulesets.yaml`](https://github.com/dflippojr/agent-ops/blob/db962ecd8c0ddbb9279324c9b18ed00efaf0ddfb/config/rulesets.yaml).
The inventory was fetched at that exact commit. Each repository was read from the
API-reported default branch at the head below, with visibility read from the API.
No target checkout, build, test or script was executed. No consumer was changed.

87 policy files were inspected. Actionlint 1.7.12 found no diagnostics in
the 16 repositories with workflows; agent-ops has no matching files. There were
**8 errors** (public self-hosted placement), **3 warnings**, no literal parallelism
violations and no fetch/read failures. The two allowlisted review jobs passed.
The review jobs reported below are outside the issue’s two-repository exception.

| Repository | Visibility | Inspected head | Policy files | Actionlint | Errors | Warnings |
| --- | --- | --- | ---: | --- | ---: | ---: |
| dflippojr/agent-harness | public | `daf8267f499520b3d942b2b45342cd8c99d822bb` | 6 | pass (5 workflows) | 2 | 0 |
| dflippojr/agent-harness-hub | public | `be06e9779af6237ff14db601cd65a35fad7d57c9` | 6 | pass (5 workflows) | 0 | 0 |
| dflippojr/financial-planner | public | `be114d26aa435ea5f40aa1f0ac628b9d453ab22e` | 9 | pass (7 workflows) | 1 | 0 |
| dflippojr/music-discovery | public | `ba593415bf582c33f6fedba6a3c4cee0491b743a` | 7 | pass (4 workflows) | 1 | 0 |
| dflippojr/fhir-crd-router | public | `fb52bff461eb55bbf5a4479e2fe67a23311af574` | 6 | pass (6 workflows) | 1 | 0 |
| dflippojr/payer-onboarding-workbench | public | `200f440a87fee1b7b5361dcf2d40618ced4e3c36` | 7 | pass (4 workflows) | 1 | 0 |
| dflippojr/integration-failure-lab | public | `7c66d3e47dc7ea8e12ffee32d38ba6c6048a165f` | 8 | pass (4 workflows) | 1 | 0 |
| dflippojr/wallpaper-rotator | public | `4870a39113e30988199f3b76a695d50791b6809e` | 5 | pass (5 workflows) | 1 | 0 |
| dflippojr/.github | public | `e2c7fae8508be0359f3abaf136caade46ee7479c` | 6 | pass (6 workflows) | 0 | 2 |
| dflippojr/open-payer-sandbox | private | `b78baf96a6239f05fb75634ff8500d9a92ad7d58` | 4 | pass (4 workflows) | 0 | 1 |
| dflippojr/task-start-copilot | private | `679bfb9df6d80a0215781c037579892b2a637c94` | 3 | pass (3 workflows) | 0 | 0 |
| dflippojr/desk-controller | private | `4bbe58ac01f562087388d72111fd8b91818a6216` | 6 | pass (6 workflows) | 0 | 0 |
| dflippojr/disc-golf-league | private | `68d9872571b8f2886ef3aaaea51214396ab2da72` | 5 | pass (4 workflows) | 0 | 0 |
| dflippojr/personal-website | private | `c2f8ed4e1d98ff5cc48b2bedaee9cdba5547e906` | 6 | pass (5 workflows) | 0 | 0 |
| dflippojr/agent-ops | private | `db962ecd8c0ddbb9279324c9b18ed00efaf0ddfb` | 0 | no workflows | 0 | 0 |
| dflippojr/agent-playbook | private | `5c7a5af347a00dbd6d440b918a44df846dddc0a3` | 2 | pass (2 workflows) | 0 | 0 |
| dflippojr/agent-memory-library | private | `0a0d28606b679acfa0cd3c3a5ffe647c24094e72` | 1 | pass (1 workflows) | 0 | 0 |

## Findings

| Repository | Severity / rule | File | Finding |
| --- | --- | --- | --- |
| dflippojr/agent-harness | error / public-self-hosted | `.github/workflows/ci-cd.yml` | deploy-tower: self-hosted is forbidden on public repositories |
| dflippojr/agent-harness | error / public-self-hosted | `.github/workflows/staging.yml` | stage: self-hosted is forbidden on public repositories |
| dflippojr/financial-planner | error / public-self-hosted | `.github/workflows/review.yml` | review: self-hosted is forbidden on public repositories |
| dflippojr/music-discovery | error / public-self-hosted | `.github/workflows/review.yml` | review: self-hosted is forbidden on public repositories |
| dflippojr/fhir-crd-router | error / public-self-hosted | `.github/workflows/review.yml` | review: self-hosted is forbidden on public repositories |
| dflippojr/payer-onboarding-workbench | error / public-self-hosted | `.github/workflows/review.yml` | review: self-hosted is forbidden on public repositories |
| dflippojr/integration-failure-lab | error / public-self-hosted | `.github/workflows/review.yml` | review: self-hosted is forbidden on public repositories |
| dflippojr/wallpaper-rotator | error / public-self-hosted | `.github/workflows/review.yml` | review: self-hosted is forbidden on public repositories |
| dflippojr/.github | warning / dynamic-runner | `.github/workflows/gate-guard-target.yml` | judge: runner expression cannot be resolved statically |
| dflippojr/.github | warning / dynamic-runner | `.github/workflows/gate-guard.yml` | gate-guard: runner expression cannot be resolved statically |
| dflippojr/open-payer-sandbox | warning / private-hosted | `.github/workflows/golden-guard-target.yml` | golden-judge: GitHub-hosted runner without the CI_RUNS_ON switch |

## Fixture and implementation proof

`python -m pytest -q tests/test_workflow_lint.py`: **54 passed**. This includes
28 named policy fixtures (runner visibility, matrix labels, JSON runner input,
CI_RUNS_ON, both review exceptions, command caps, two-worker and four-worker
counts, shell continuations, package scripts, comments and malformed files),
plus cap boundaries, exception expiry/path/job/guard scoping, JSON Unicode
escapes, Python/JS command literals, safe git reads and changed-file selection.

Real actionlint was run with the trusted config and external subprocesses disabled:

```text
actionlint -shellcheck= -pyflakes= -config-file config/actionlint.yaml tests/fixtures/workflow_lint/actionlint-valid.yml
exit: 0

actionlint -shellcheck= -pyflakes= -config-file config/actionlint.yaml tests/fixtures/workflow_lint/actionlint-invalid.yml
tests/fixtures/workflow_lint/actionlint-invalid.yml:6:24: undefined variable "does_not_exist" [expression]
exit: 1
```

Both new workflows also passed real actionlint. The local release archive’s
SHA256 matched the publisher’s checksums:
`6e7241b51e6817ea6a047693d8e6fed13b31819c9a0dd6c5a726e1592d22f6e9`.
CI builds actionlint from commit
`914e7df21a07ef503a81201c76d2b11c789d3fca` (v1.7.12).

## Reproduce

Fetch the inventory at the commit linked above, install the pinned actionlint,
and supply a read-only GitHub token able to read the private repositories:

```sh
python scripts/audit_workflow_lint.py --rulesets rulesets.yaml --rulesets-revision db962ecd8c0ddbb9279324c9b18ed00efaf0ddfb --actionlint actionlint --output audit.json
```

The JSON records each visibility/head, every policy finding, actionlint’s file
list/output/exit code and explicit repository read failures. Violations do not
stop the inventory; audit read failures make the audit command exit nonzero.

## Not verified

No consumer adoption or changes to the flagged workflows, self-hosted runner
execution, real fork PR or expiry-day clock transition was attempted. The
expiry boundary and guard removal are tested with fixed dates and fixture data.
The audit is a snapshot; later commits can change findings. No tests in target
repositories were run. Runtime variables, arbitrary shell computation, argument
arrays assembled by wrappers and code inside remote reusable workflows cannot
be fully resolved by this static check. Unresolved runner/count expressions warn.
Only workflow run steps, shell/PowerShell ci-local commands, Python/JS command
string literals and package.json script strings are inspected for CLI counts.
