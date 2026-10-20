# Issue #35 evidence: where a posted gate-guard check run lands

Read-only REST and GraphQL calls on 2026-10-19/20, with the workers' token.
personal-website is private and the token can't read it, so runs 39345117867
and 39351414882 named in the issue were not inspected. The public repositories
below show the same mechanism.

## GitHub picks the suite, and it is not the run's own

`gate_guard.py --pull-request-target` posted its `gate-guard` check run with
`POST /repos/{repo}/check-runs`, which takes no check suite id. Every such
check run on a head went into the head's oldest GitHub Actions check suite,
whichever workflow and event that suite belonged to.

wallpaper-rotator 4f90ff8 (PR #35, merged):

| Suite | Workflow run | Event | Check runs in it |
| --- | --- | --- | --- |
| 106369809396 (12:49:00, oldest) | Release 39255007064 | workflow_dispatch | all five `gate-guard` runs (failure, failure, success, success, neutral), plus Release's own jobs |
| 106369821362 | Gate guard (default branch) 39255011550 | pull_request_target | `judge / judge` only |
| 106374822122 | Gate guard (default branch) 39256791921 | pull_request_target | `judge / judge` only |
| 106421363083 | Gate guard (default branch) 39273355978 | pull_request_target | `judge / judge` only |
| 106579934212 | Gate guard (default branch) 39329913413 | pull_request_target | `judge / judge` only |

`check-runs?check_name=gate-guard&filter=all` returns those five runs, all in
suite 106369809396. The commit's `statusCheckRollup`, which is what the pull
request shows and a ruleset evaluates, holds every `judge / judge` and the CI
checks, but **no `gate-guard`**: the suite that carries it isn't part of the
pull request's checks. A ruleset requiring `gate-guard` there would wait
forever, as personal-website#240 did after its `labeled` re-run.

financial-planner b15c81d (PR #559): both runs' `gate-guard` check runs
(118401705905, 118404833600) went into suite 106641055851, the first gate-guard
run's suite, not the second run's 106643612028, which holds only its
`judge / judge`. Here the rollup still lists `gate-guard`; whether GitHub hides
the older suite depends on things the guard doesn't control.

## A job's own check stays with its run

Every `judge / judge` check run above sits in its own workflow run's suite and
appears in the rollup. Its REST record has no output text
(`output.summary` and `output.title` are null) but carries its annotations
(`/check-runs/{id}/annotations`), which is why the guard now states its verdict
as a notice annotation for `pr_body_lint.py`.

Commands, for reproduction:

```sh
gh api "repos/dflippojr/wallpaper-rotator/commits/4f90ff8810c45ed5c3ecb0c31b10a44f52778d80/check-runs?filter=all&check_name=gate-guard" \
  --jq '.check_runs[] | {id, conclusion, suite: .check_suite.id}'
gh api graphql -f query='query{repository(owner:"dflippojr",name:"wallpaper-rotator"){object(oid:"4f90ff8810c45ed5c3ecb0c31b10a44f52778d80"){... on Commit{
  statusCheckRollup{contexts(first:100){nodes{... on CheckRun{name checkSuite{databaseId}}}}}
  checkSuites(first:30){nodes{databaseId workflowRun{databaseId event workflow{name}}}}}}}}'
```
