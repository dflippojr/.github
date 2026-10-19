# Reusable secret scan

Issue #22 adds `.github/workflows/secret-scan.yml` and
`templates/secret-scan-caller.yml`. Copying callers into other repositories is
separate gate-wiring work. The job is named **secret-scan** (GitHub can prefix
the caller job name in the Checks UI).

Pin the reusable workflow and its `scanner-ref` input to the same reviewed
40-character commit SHA. All actions in this workflow are SHA-pinned. Gitleaks
8.30.1 is built from source commit
`83d9cd684c87d95d656c1458ef04895a7f1cbd8e`, using Go's module checksum
verification. Build parallelism is capped at four. Ubuntu runners need Python 3,
Git and Go with toolchain download support. No license secret is needed for
the open-source CLI. See the [upstream CLI documentation](https://github.com/gitleaks/gitleaks).

## Private identifiers

Use a repository Actions secret named **SECRET_SCAN_DENYLIST_SHA256**, passed
explicitly by the caller. Its value is whitespace-separated lowercase SHA-256
hashes of the owner's hostnames, email addresses, Windows user directory and
macOS user directory. Include every hostname alias and email alias. The secret
is required and malformed or empty configuration fails the scan; there is no
silent opt-out. For an organization, an organization Actions secret scoped to
the caller repositories can supply the same value. These repositories currently
belong to a personal account, so use repository secrets.

We chose secret storage even for the hashes: publishing unsalted hashes would
permit dictionary attacks against guessable usernames and machine names.
Neither raw identifiers nor their hashes belong in source, issue comments,
PR descriptions or logs. The owner prepares the initial list privately; no
real identifier inventory was collected or published for this change.

For each token, normalization applies Unicode NFKC, case folding, backslashes
to slashes, collapsing repeated slashes, and removal of trailing dots/slashes.
Hash that UTF-8 string with SHA-256. A trusted local Python session can import
`digest` from `scripts.secret_scan`; read tokens with `getpass.getpass`, and
write the hashes directly to a private file for secret upload. Do not pass raw
identifiers on a command line or print them. Delete that private file afterward.

The scanner extracts complete words, addresses, hostnames, path components and
every path prefix, so a directory match also flags files below it. It also flags
all `*.ts.net` names, RFC1918 IPv4 and IPv6 ULA addresses without an inventory.
Documentation IP ranges and localhost are allowed. This is token matching,
not arbitrary substring matching: encodings other than normalization above,
split strings and encrypted data are not decoded.

Fork and Dependabot PRs generally cannot receive Actions secrets. They fail
closed when the denylist is absent. A maintainer must arrange a trusted scan
before merging; do not work around this with `pull_request_target` or expose a
secret to untrusted code. Caller pin changes need normal gate review.

## Commit scope and redaction

The scanner fetches the PR's base and head into a temporary bare Git repository,
finds their merge base, and runs gitleaks across `merge-base..head`. Personal
identifiers are checked on added lines in each commit in the same range,
including intermediate commits whose content was later removed. Added or
changed file paths are also checked. Merge commits are conservatively compared
with their first parent. The scan does not examine unrelated base history.

No PR code is checked out or executed. There are no submodules, LFS downloads,
build scripts, hooks, textconv or external diff drivers from the PR. Gitleaks
uses its trusted built-in rules, ignores inline `gitleaks:allow` comments and
reads the bare object database, so PR `.gitleaksignore` and `.gitleaks.toml`
files cannot suppress results. Local repository allowlists are deliberately
unsupported. The token only has `contents: read` and is passed to Git via its
environment, never a command-line argument or saved credential.

Gitleaks stdout/stderr and its temporary report are never logged or uploaded.
The report is generated with full redaction and deleted when scanning finishes.
Only JSON-escaped **file, line and rule** are emitted, followed by a finding
count. Paths containing denied identifiers are replaced by a stable SHA-256
path reference, so a private hostname in a filename is not leaked. Errors
produce a generic incomplete-scan failure, never raw exception
or subprocess output. Exit codes: 0 clean, 1 findings, 2 incomplete/error.
Text diffs are covered; binary/encrypted containers and arbitrary encodings
need separate inspection. This check complements push protection.

## Financial-planner content scan

Set `financial: true` in the financial-planner caller. This additionally scans
all tracked files at the head whose path includes `test`, `tests`, `fixtures`
or `__tests__`, or whose filename begins `test_`. Files are read as Git objects;
symlinks are never followed. Fixture files (under `fixtures/`, or named
`*_fixtures.*`) must have the uppercase word `SYNTHETIC` within their first five
lines. Other test source files are scanned for numbers but need no marker.

It reports Luhn-valid 13–19 digit runs, ABA-checksum-valid nine-digit runs,
and any account-like run of ten or more digits. Spaces and hyphens within a
number are supported. These are checksum checks, not issuer/account validation:
even all-zero numbers with valid checksums are flagged. A marker is mandatory provenance,
**not an exemption**: synthetic examples with valid checksums still fail.
An invalid Luhn checksum can still match the independent account rule.

Run the content scan independently against an existing checkout or bare clone:

```sh
python scripts/secret_scan.py --repo-dir ../financial-planner --head origin/main --financial-only
```

Existing findings require financial-planner remediation; this change neither
rewrites history nor changes its files. Main-scan evidence is recorded in the
PR Proof table. Fix any synthetic-marker and number findings in its gate-wiring
issue before enabling the financial job.

## Owner setup

For each caller: **Settings → Secrets and variables → Actions → New repository
secret**, name `SECRET_SCAN_DENYLIST_SHA256`. The owner must populate the private
inventory before wiring callers. No production inventory has been verified.

For every repository below: **Repository → Settings → Security and quality →
Advanced Security → Secret Protection → Enable**, then **Secret Protection →
Push protection → Enable**. See [GitHub's push protection setup](https://docs.github.com/en/code-security/how-tos/secure-your-secrets/prevent-future-leaks/enable-push-protection).
The owner must perform this security-settings step; an unavailable control on
a private repository needs an eligible GitHub Secret Protection plan.

Inventory taken from `gh repo list dflippojr --limit 200` for issue #22:

| Active repositories | |
| --- | --- |
| .github | agent-harness |
| agent-harness-hub | agent-memory-library |
| agent-ops | agent-playbook |
| AIConcepts | ClashAPI |
| Crypt | CVTasks |
| DataMining | desk-controller |
| DflippoQuandary | disc-golf-league |
| fhir-crd-router | financial-planner |
| Flippo.5 | HnH |
| integration-failure-lab | media-tracker-sync |
| music-discovery | nexapro |
| ntfy-homelab | observability-stack |
| open-payer-sandbox | payer-onboarding-workbench |
| personal-website | plex-webhook |
| searxng-homelab | sonarqube-homelab |
| stables | task-start-copilot |
| TrackingProject | UNIXScripts |
| wallpaper-rotator | |

Archived repositories: `agent-loop`, `agent-loop-private`. Confirm protection
when unarchiving; archived repositories are read-only. Repo security settings
were not changed or asserted enabled by this implementation.
