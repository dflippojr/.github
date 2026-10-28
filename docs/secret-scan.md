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
every path prefix, so a directory match also flags files below it. Mailboxes are
also extracted independently of URI schemes, including Markdown/HTML `mailto:`
links, without dropping whole-path or hostname candidates. It also flags
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
finds their merge base, and runs gitleaks across `merge-base..head` with
`--diff-merges=first-parent`. Merge-result patches are included while all side
commits remain traversed, including secrets removed before a merge. Personal
identifiers are checked on added lines in each commit in the same range,
including intermediate commits whose content was later removed. Added or
changed file paths are also checked. Merge commits are conservatively compared
with their first parent. The scan does not examine unrelated base history.

No PR code is checked out or executed. There are no submodules, LFS downloads,
build scripts, hooks, textconv or external diff drivers from the PR. Gitleaks
uses its trusted built-in rules, ignores inline `gitleaks:allow` comments and
reads the bare object database, so PR `.gitleaksignore` and `.gitleaks.toml`
files cannot suppress results. The scanned repository cannot allowlist
anything; the only exemptions are reviewed entries in this repository's
financial exemption manifest (see below). The token only has `contents: read` and is passed to Git via its
environment, never a command-line argument or saved credential.

Gitleaks stdout/stderr and its temporary report are never logged or uploaded.
The report is generated with full redaction and deleted when scanning finishes.
Only JSON-escaped **file, line and rule** are emitted, followed by a finding
count. Every file field is a stable `<redacted-path:SHA256>` reference computed
from the UTF-8 repository-relative path, including in financial-only mode. Raw
filenames are never printed: they can contain identifiers, API keys or financial
numbers independently of their contents. To locate a finding, compare path hashes
privately in a trusted checkout; do not publish that mapping. Errors
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
**not an exemption**: synthetic examples with valid checksums still fail unless a
reviewed manifest entry covers them. An invalid Luhn checksum can still match the
independent account rule, and every Luhn-valid number is also account-like, so
exempting one needs an entry for each rule.

### Reviewed exemptions

`config/secret-scan-exemptions.json` in this repository holds the only
exemptions. The scanner reads it from its own checkout
(`<script dir>/../config/secret-scan-exemptions.json`), never from the scanned
repository, and no CLI flag picks another file. It is committed as `[]`. Each
entry is an object with exactly these fields:

| Field | Value |
| --- | --- |
| `repository` | `owner/name` of the caller |
| `path_sha256` | The 64 lowercase hex characters inside `<redacted-path:…>` |
| `rule` | `luhn-number`, `aba-routing-number`, `account-like-number` or `missing-synthetic-header` |
| `max_count` | A positive integer: how many findings of that rule the file may have |
| `reason` | Why the values are deliberate synthetic test data |

```json
[
  {
    "repository": "owner/name",
    "path_sha256": "<64 lowercase hex>",
    "rule": "luhn-number",
    "max_count": 2,
    "reason": "Masking test needs a card-shaped synthetic value"
  }
]
```

Only entries whose `repository` matches `--repository` apply. It defaults to
`GITHUB_REPOSITORY`, which in a reusable workflow names the caller. With neither
set, no exemption applies. When the financial scan runs, the scanner drops up to
`max_count` matching financial findings per entry; findings above the count,
under another rule, and every gitleaks or identifier finding still fail.
Exempt findings are never printed, and the summary becomes
`secret-scan: N finding(s), M exempt`, where N counts only failing findings.

The scan fails closed with exit 2 when the manifest is malformed (not a JSON
array, missing or extra fields, a duplicate field, an unknown rule, a bad
`repository` or `path_sha256`, a `max_count` that isn't a positive integer, an
empty `reason`), when two entries share a (repository, path_sha256, rule) key,
or when an entry for the scanned repository is stale: its file has fewer
findings under that rule at head than `max_count`, including a file with no
findings or no file at all. Lower or remove the entry when the values go away.
A missing manifest file applies no exemptions, so findings still fail.

Compute `path_sha256` privately in a trusted checkout of the caller:

```sh
printf %s "<repo-relative path>" | sha256sum
```

Don't publish the mapping from path to hash. Every exemption is reviewed in a
pull request to this repository, followed by a caller `scanner-ref` bump to
the merged commit. The scanned pull request can't change either. The trusted
scanner checkout in `.github/workflows/secret-scan.yml` must include
`config/secret-scan-exemptions.json` in its sparse-checkout list; without it,
CI applies no exemptions and fails closed.

Run the content scan independently against an existing checkout or bare clone:

```sh
python scripts/secret_scan.py --repo-dir ../financial-planner --head origin/main --financial-only --repository dflippojr/financial-planner
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
