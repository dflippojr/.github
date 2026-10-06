# How work flows in these repos

Feature work and fixes go through GitHub issues and pull requests, so plans and history live in one reviewable place. Coding agents follow the same flow as people.

## Issues and labels

| Label | Meaning |
| --- | --- |
| `needs-refinement` | Needs product or design decisions before work can start |
| `ready` | Specified enough to implement or verify without further decisions |
| `blocked` | Specified, but waiting on a dependency |
| `user-present` | The owner or a physical device is needed for the next step |
| `in-progress` | Work started on a branch; no pull request yet |
| `awaiting-review` | Pull request open; waiting for the owner's review or merge |
| `verification` | Confirm something already built works for real |
| `reserved` | Held for a specific person or run; agents and orchestrators skip it |
| `parked` | Deliberately deferred; not planned for now |
| `P0` / `P1` / `P2` | Priority: do first / bugs and fixes ahead of features / normal |

An issue moves from `needs-refinement` to `ready` once its outcome, scope, decisions and acceptance criteria are written down. Only `ready` issues without `reserved`, `parked`, `blocked` or `user-present` get picked up.

The labels themselves are defined in `labels.json`. `python scripts/sync_labels.py` shows what differs in each active repository (not archived, not a fork, pushed within the last 365 days; `--since YYYY-MM-DD` overrides the cutoff), and `--apply` brings them in line. Naming a repository that doesn't exist is an error (exit 2) before anything is changed.

## Writing an issue

Start from the feature template. A ready issue has these sections:

- **Outcome**: what is true once the work is done, in a sentence or two.
- **Background**: why it matters and where it touches the code.
- **Scope**: what is in and what is out.
- **Decisions**: calls already made, each written as a recommended default the owner can override ("Do X. Override if you want Y."), not as an open question.
- **Dependencies**: issues or PRs that must land first, or "None".
- **Acceptance criteria**: a checklist someone else can verify.

Questions only the owner can answer go under an `Open questions` heading, each with a recommended answer, instead of being guessed. The issue stays `needs-refinement` until they are answered. If the owner approves the issue without answering, the recommended answers stand.

Add a priority when refining, not when filing: `P0` for anything that must be done first, `P1` for bugs and fixes ahead of features, `P2` for everything else. Templates do not set one.

## Branches

Keep it simple and suit the project. A short prefix plus the issue number and a slug is a good default, for example `feat/12-retry-alerts`, `fix/30-null-title` or `docs/8-backlog-sync`. Branch from the default branch, keep one issue per branch, and delete the branch after merge.

## Pull requests

- Link the issue with `Closes #N` so merging closes it.
- Say what changed, how it was verified, and anything the owner has to do after merging.
- The owner reviews and merges every pull request. Agents push branches and open PRs only with the owner's go-ahead; they never merge or close issues themselves.
- Never commit secrets, tokens, device identifiers or private paths, even in private repos.
