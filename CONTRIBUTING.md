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
| `P0` / `P1` / `P2` | Priority: do first / bugs and fixes ahead of features / normal |

An issue moves from `needs-refinement` to `ready` once its outcome, scope, decisions and acceptance criteria are written down. Only `ready` issues get picked up.

## Branches

Keep it simple and suit the project. A short prefix plus the issue number and a slug is a good default, for example `feat/12-retry-alerts`, `fix/30-null-title` or `docs/8-backlog-sync`. Branch from the default branch, keep one issue per branch, and delete the branch after merge.

## Pull requests

- Link the issue with `Closes #N` so merging closes it.
- Say what changed, how it was verified, and anything the owner has to do after merging.
- The owner reviews and merges every pull request. Agents push branches and open PRs only with the owner's go-ahead; they never merge or close issues themselves.
- Never commit secrets, tokens, device identifiers or private paths, even in private repos.
