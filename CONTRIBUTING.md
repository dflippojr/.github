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

The remaining labels are GitHub's defaults plus one for accessibility:

| Label | Meaning |
| --- | --- |
| `bug` | Something isn't working |
| `documentation` | Improvements or additions to documentation |
| `duplicate` | This issue or pull request already exists |
| `enhancement` | New feature or request |
| `good first issue` | Good for newcomers |
| `help wanted` | Extra attention is needed |
| `invalid` | This doesn't seem right |
| `question` | Further information is requested |
| `wontfix` | This will not be worked on |
| `accessibility` | Barrier affecting people with disabilities |

An issue moves from `needs-refinement` to `ready` once its outcome, scope, decisions and acceptance criteria are written down. Only `ready` issues without `reserved`, `parked`, `blocked` or `user-present` get picked up.

The labels themselves are defined in `labels.json`. `python scripts/sync_labels.py` shows what differs in each active repository (not archived, not a fork, pushed within the last 365 days; `--since YYYY-MM-DD` overrides the cutoff), and `--apply` brings them in line. Naming a repository that doesn't exist is an error (exit 2) before anything is changed.

## Branches

Keep it simple and suit the project. A short prefix plus the issue number and a slug is a good default, for example `feat/12-retry-alerts`, `fix/30-null-title` or `docs/8-backlog-sync`. Branch from the default branch, keep one issue per branch, and delete the branch after merge.

## Pull requests

- Link the issue with `Closes #N` so merging closes it.
- Say what changed, how it was verified, and anything the owner has to do after merging.
- The owner reviews and merges every pull request. Agents push branches and open PRs only with the owner's go-ahead; they never merge or close issues themselves.
- Never commit secrets, tokens, device identifiers or private paths, even in private repos.
