# PR #30 review-fix proof

Reviewed baseline: 03a52e6adfe681064c2923cfee7a6dd3710c829a.
Tests committed first at 0b148ae, with the baseline scanner unchanged.

Validation used a disposable Python 3.12 container with no network, a read-only
workspace/root filesystem, non-root user, dropped capabilities, no-new-privileges,
4 CPUs, 1 GiB memory, 128-process limit and a disposable /tmp. Pytest ran serially;
GOMAXPROCS was 4. Only synthetic repositories/values were used. Test diagnostics
were captured; only safe result summaries were emitted. No raw matched identifiers,
keys or sensitive filenames are included here.

Gitleaks v8.30.1's tag resolves to the workflow pin
83d9cd684c87d95d656c1458ef04895a7f1cbd8e. The Linux x64 release archive was verified
against the release's SHA-256 checksums before running the real CLI.

| Finding | Red with unchanged baseline scanner | Fix | Green |
| --- | --- | --- | --- |
| Merge-only secrets | test_gitleaks_log_options_cover_merge_results_and_side_commits: 1 failed; real-gitleaks merge proof exit 1 | c4b7bcb: explicit first-parent merge diffs, without restricting commit traversal | Git patch regression passes; real CLI catches merge-only and removed side-branch PATs |
| Sensitive filenames | test_sensitive_filename_output_is_opaque: 3 failed (PAT, financial, financial-only) | a447341: every output filename is an opaque SHA-256 reference | All 3 captured-output cases pass; raw synthetic token/number absent from stdout/stderr |
| mailto email bypass | test_mailto_matches_denied_mailbox: 4 failed; 3 negative cases already passed | 8b8e718: extract normalized mailbox candidates independently of URI schemes | All 7 mailto positive/negative cases pass |

Full-suite command: python -m pytest -q --cov=scripts --cov-report=xml:/tmp/coverage.xml
Result: **141 passed, 72 subtests passed**.

Integration command: python tests/prove_secret_scan.py /usr/local/bin/gitleaks
Result: exit 0, all 5 scenarios pass:

- Merge-only and removed side-branch PATs caught.
- Base-only secrets excluded.
- Removed linear-commit PAT caught despite PR allow/config/ignore files.
- Actual gitleaks and identifier CLI output contains no matched values.
- Actual gitleaks and both financial modes keep sensitive filenames opaque.

The new pytest regressions run in the existing CI test job. The separate real-CLI
proof was run locally in the constrained container; the CI workflow was not changed.
The earlier scratch-PR proof predates these fixes and is historical evidence only.
