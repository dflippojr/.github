#!/usr/bin/env python3
"""Turn the owner's `/approve-gate <head sha>` pull request comment into gate-change-approved.

The approve-gate workflow runs this on an issue_comment created event, from the
calling repository's default branch. It never checks out or runs pull request
code. It reads the comment and the pull request again from the API, and acts
only when all of these hold:

    the comment's author is the repository owner, by login and by user id
    the comment was never edited, and the event is `created`
    the body is exactly `/approve-gate <sha>`, a full 40-character lowercase SHA (trailing whitespace aside)
    the comment is on a pull request, open, whose head branch is in this repository
    the SHA is the pull request's current head

It then removes gate-change-approved if it is already on (so GitHub records a
new labeled event, dated after the newest push), adds it, and posts the head
record `ao owner-label` writes: <!-- owner-label gate-change-approved head=SHA -->.
Label first, then record, so `ao ready` finds the record at or after the label.
Both calls use OWNER_GATE_TOKEN, the owner's own token, so GitHub records the
owner as the actor and the gate guard and `ao ready` need no new rule. The
script checks that the token is the owner's before it writes.

Last it reads the head again. A push that landed between the first read and
the label would be dated before the label, and the gate guard would count the
label for a commit the owner never named, so when the head has moved the label
is removed again. For the same reason the label is removed again whenever the
run fails once the label request is sent (the label write itself, the record,
the final read). If that removal fails too, the run says so in an error line
naming the pull request and exits 2: the owner removes the label by hand.

Exit status: 0 applied, 1 refused, 2 when the API could not be read or written.
A refusal of the owner's own comment is answered with a reply on the pull
request; anyone else's comment is refused silently.

    OWNER_GATE_TOKEN=... python3 approve_gate.py
"""
import http.client
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

LABEL = "gate-change-approved"
COMMAND = re.compile(r"/approve-gate ([0-9a-f]{40})")
SHA = re.compile(r"[0-9a-f]{40}")


class Refused(Exception):
    """The comment does not approve anything; the message says why."""


class LabelLeft(Exception):
    """The label may be on a pull request the run did not approve, and removing it failed."""


class GitHub:
    """The GitHub API calls this script makes, all with one token."""

    def __init__(self, api, token):
        self.api, self.token = api, token

    def request(self, method, path, body=None):
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "approve-gate", "Authorization": f"Bearer {self.token}"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.api + path, data=data, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8") or "null")

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, body):
        return self.request("POST", path, body)

    def delete(self, path):
        return self.request("DELETE", path)


def requested_sha(comment, owner, owner_id):
    """The SHA the owner's comment names. Raises Refused for any other author, an edit or any other body."""
    user = comment.get("user") or {}
    login, uid = user.get("login"), user.get("id")
    if login != owner or uid != owner_id or user.get("type") != "User":
        raise Refused(f"the comment is by @{login} (id {uid}), not the owner @{owner} (id {owner_id})")
    if not comment.get("created_at") or comment.get("updated_at") != comment.get("created_at"):
        raise Refused("the comment was edited; post a new one")
    body = (comment.get("body") or "").strip()
    match = COMMAND.fullmatch(body)
    if not match:
        raise Refused("the comment must be exactly `/approve-gate <sha>` with the full 40-character head SHA")
    return match.group(1)


def check_pull_request(issue, pull, repo, sha):
    """Raises Refused unless ISSUE is an open pull request of REPO, from REPO, whose head is SHA."""
    if not issue.get("pull_request"):
        raise Refused("this is an issue, not a pull request")
    if pull.get("state") != "open":
        raise Refused("the pull request is not open")
    if ((pull.get("base") or {}).get("repo") or {}).get("full_name") != repo:
        raise Refused("the pull request's base is not this repository")
    head = pull.get("head") or {}
    head_repo = (head.get("repo") or {}).get("full_name")
    if head_repo != repo:
        raise Refused(f"the pull request's head is in {head_repo or 'a deleted repository'}, not {repo}; "
                      "fork pull requests are not approved by comment")
    if head.get("sha") != sha:
        raise Refused(f"the pull request's head is now {head.get('sha')}, not {sha}; review the new commits "
                      "and approve that head")


def head_record(sha, comment_url):
    return (f"Owner approved `{LABEL}` at head {sha} by [comment]({comment_url}). A later push invalidates "
            f"it.\n\n<!-- owner-label {LABEL} head={sha} -->")


def remove_label(github, issue):
    """Take the label off; one already gone (GitHub's 404) is off."""
    try:
        github.delete(f"{issue}/labels/{urllib.parse.quote(LABEL)}")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise


def apply(github, repo, number, sha, comment_url):
    """Label, then record, then make sure the head did not move meanwhile. (code, message). Unless the head is
    still sha at the end, the label comes off again, whatever failed; LabelLeft when that removal fails."""
    issue = f"/repos/{repo}/issues/{number}"
    names = {(label.get("name") or "").lower() for label in github.get(f"{issue}/labels?per_page=100")}
    if LABEL in names:
        # Adding a label the pull request has records no event, and the gate guard dates the label by its event.
        github.delete(f"{issue}/labels/{urllib.parse.quote(LABEL)}")
    now = None
    try:
        # A push can land between the check and the label, so the label is the owner's only once the head is
        # read back unchanged; until then any failure (even of the label request: GitHub may have done it) undoes it.
        github.post(f"{issue}/labels", {"labels": [LABEL]})
        github.post(f"{issue}/comments", {"body": head_record(sha, comment_url)})
        now = (github.get(f"/repos/{repo}/pulls/{number}").get("head") or {}).get("sha")
    finally:
        if now != sha:
            try:
                remove_label(github, issue)
            except Exception as e:
                raise LabelLeft(f"{LABEL} is still on {repo}#{number}, which this run did not approve, and removing "
                                f"it failed ({e}); remove it by hand before anything merges") from e
    if now != sha:
        github.post(f"{issue}/comments", {"body": (
            f"`{LABEL}` removed again: the head moved from {sha} to {now} while it was applied. Review the "
            "new commits and approve that head.")})
        return 1, f"the head moved to {now} while {LABEL} was applied; the label was removed"
    return 0, f"applied {LABEL} to {repo}#{number} at head {sha}"


def run(github, event_name, event, repo, owner, owner_id):
    """(code, message) for one issue_comment event."""
    if event_name != "issue_comment" or event.get("action") != "created":
        return 1, f"only issue_comment created events approve, not {event_name} {event.get('action')}"
    number = (event.get("issue") or {}).get("number")
    comment_id = (event.get("comment") or {}).get("id")
    if not isinstance(number, int) or not isinstance(comment_id, int):
        return 1, "the event names no issue and comment"
    comment = github.get(f"/repos/{repo}/issues/comments/{comment_id}")
    if not (comment.get("issue_url") or "").endswith(f"/repos/{repo}/issues/{number}"):
        return 1, f"comment {comment_id} is not on {repo}#{number}"
    try:
        sha = requested_sha(comment, owner, owner_id)
    except Refused as e:
        if (comment.get("user") or {}).get("id") != owner_id:
            return 1, str(e)  # someone else's comment gets no reply
        sha, refused = None, e
    else:
        refused = None
    me = github.get("/user")
    if me.get("login") != owner or me.get("id") != owner_id:
        return 2, f"OWNER_GATE_TOKEN belongs to @{me.get('login')}, not the owner @{owner}"
    if refused is None:
        try:
            issue = github.get(f"/repos/{repo}/issues/{number}")
            pull = github.get(f"/repos/{repo}/pulls/{number}") if issue.get("pull_request") else {}
            check_pull_request(issue, pull, repo, sha)
        except Refused as e:
            refused = e
    if refused is not None:
        github.post(f"/repos/{repo}/issues/{number}/comments",
                    {"body": f"`{LABEL}` was not applied: {refused}."})
        return 1, str(refused)
    return apply(github, repo, number, sha, comment.get("html_url") or "")


def escape(text):
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def main():
    token = os.environ.get("OWNER_GATE_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "")
    owner_id = os.environ.get("GITHUB_REPOSITORY_OWNER_ID", "")
    if not token:
        print("::error::OWNER_GATE_TOKEN is not set; the owner adds it as a secret of the repository's "
              "approve-gate environment (README.md, \"Approve a gate change by comment\").")
        return 2
    if not repo or not owner or not owner_id.isdigit():
        print("::error::GITHUB_REPOSITORY, GITHUB_REPOSITORY_OWNER and GITHUB_REPOSITORY_OWNER_ID must be set.")
        return 2
    path = os.environ.get("GITHUB_EVENT_PATH")
    with open(path, encoding="utf-8") as f:
        event = json.load(f)
    github = GitHub(os.environ.get("GITHUB_API_URL", "https://api.github.com"), token)
    try:
        code, message = run(github, os.environ.get("GITHUB_EVENT_NAME"), event, repo, owner, int(owner_id))
    except LabelLeft as e:
        code, message = 2, str(e)
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError, KeyError, TypeError,
            AttributeError) as e:
        code, message = 2, f"could not read or write the pull request ({e})"
    print(("::notice::" if code == 0 else "::error::") + escape(message))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"## Approve gate\n\n{message}\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
