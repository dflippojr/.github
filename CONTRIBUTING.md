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
| `gate-change-approved` | The owner approved this pull request's test removals, skips, retries or gate-config edits; the gate guard then passes |
| `findings-accepted` | The owner accepted this pull request's open review findings |
| `P0` / `P1` / `P2` | Priority: do first / bugs and fixes ahead of features / normal |

`gate-change-approved` and `findings-accepted` are owner-only. The default-branch gate guard counts one only when the owner applied it after the newest push. A label a worker applies, or re-applies, doesn't count, and a push voids the owner's label until the owner applies it again.

The owner approves a gate change with one pull request comment, `/approve-gate <full head SHA>`; a workflow then applies `gate-change-approved` and records the head as the owner (README.md, "Approve a gate change by comment"). Agents never post that comment.

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

## Writing an issue

Start from the feature form. A ready issue has these sections:

- **Outcome**: what is true once the work is done, in a sentence or two.
- **Background**: why it matters and where it touches the code.
- **Scope**: what is in and what is out.
- **Decisions**: calls already made, each written as a recommended default the owner can override ("Do X. Override if you want Y."), not as an open question.
- **Dependencies**: issues or PRs that must land first, or "None".
- **Acceptance criteria**: a checklist someone else can verify.

A bug starts from the bug form. It also asks which PR or commit introduced the bug (found with `git log -S`, blame or bisect, or "unknown") and which gate should have caught it, so the rate of defects that get past the checks can be measured.

Questions only the owner can answer go under an `Open questions` heading, each with a recommended answer, instead of being guessed. The issue stays `needs-refinement` until they are answered. If the owner approves the issue without answering, the recommended answers stand.

Add a priority when refining, not when filing: `P0` for anything that must be done first, `P1` for bugs and fixes ahead of features, `P2` for everything else. Templates do not set one.

## Branches

Keep it simple and suit the project. A short prefix plus the issue number and a slug is a good default, for example `feat/12-retry-alerts`, `fix/30-null-title` or `docs/8-backlog-sync`. Branch from the default branch, keep one issue per branch, and delete the branch after merge.

## Pull requests

- Link the issue with `Closes #N` so merging closes it.
- Say what changed and anything the owner has to do after merging. Under Proof, give each acceptance criterion a row with how you verified it and the evidence; list what you didn't verify, and any test removed or skipped or config loosened, with the reason.
- The owner reviews and merges every pull request. Agents push branches and open PRs only with the owner's go-ahead; they never merge or close issues themselves.
- Never commit secrets, tokens, device identifiers or private paths, even in private repos.
