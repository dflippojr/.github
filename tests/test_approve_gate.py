"""approve_gate.py: only the owner's own unedited `/approve-gate <head sha>` on a same-repo PR labels it."""
import http.client
import io
import json
import os
import pathlib
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import approve_gate  # noqa: E402

WORKFLOWS = ROOT / ".github" / "workflows"
TEMPLATE = ROOT / "templates" / "approve-gate-caller.yml"
OWNER, OWNER_ID, REPO, NUMBER = "dflippojr", 93831415, "dflippojr/demo", 7
HEAD, NEWER = "a" * 40, "b" * 40
CREATED = "2026-10-27T12:00:00Z"
LABEL = "gate-change-approved"
RECORD = re.compile(r"<!-- owner-label (\S+) head=([0-9a-f]{40}) -->")
DROP = object()  # in FakeGitHub(history=...): leave this key out of the GraphQL node


class FakeGitHub:
    """Answers approve_gate's API calls from a small model of one pull request, and logs every write.

    heads: the PR head each successive GET of the pull request returns (the last one repeats), so a push
    between the check and the label can be simulated. faults: (method, path) -> {n: when}, the nth such call
    raises URLError; a write with when "after" takes effect first (GitHub did it, the response was lost), one with
    "before" does not. Removing a label the issue does not have is a 404, as on GitHub.

    history: what GraphQL answers for the command's edit history. None is a never-edited node; a dict changes
    the node's keys (DROP leaves one out); an exception is raised; a callable gets the variables and returns
    the whole data.
    """

    def __init__(self, body=None, login=OWNER, uid=OWNER_ID, updated=CREATED, is_pr=True, head_repo=REPO,
                 state="open", labels=(), heads=(HEAD,), token_login=OWNER, token_id=OWNER_ID, kind="User",
                 faults=None, history=None, node_id="IC_55"):
        self.comment = {"id": 55, "node_id": node_id, "user": {"login": login, "id": uid, "type": kind},
                        "body": f"/approve-gate {HEAD}" if body is None else body,
                        "created_at": CREATED, "updated_at": updated,
                        "issue_url": f"https://api.github.com/repos/{REPO}/issues/{NUMBER}",
                        "html_url": f"https://github.com/{REPO}/pull/{NUMBER}#issuecomment-55"}
        self.is_pr, self.head_repo, self.state = is_pr, head_repo, state
        self.labels, self.heads = list(labels), list(heads)
        self.me = {"login": token_login, "id": token_id}
        self.writes = []
        self.faults, self.calls = faults or {}, {}
        self.history, self.graphql_calls = history, []

    def call(self, method, path):
        """Counts the call; the time this call fails ("before", "after" or None)."""
        n = self.calls[method, path] = self.calls.get((method, path), 0) + 1
        return self.faults.get((method, path), {}).get(n)

    def fail(self, method, path, when, now):
        if when == now:
            raise approve_gate.urllib.error.URLError(f"{method} {path} failed {when} GitHub acted")

    def get(self, path):
        self.fail("GET", path, self.call("GET", path), "before")
        if path == f"/repos/{REPO}/issues/comments/55":
            return self.comment
        if path == "/user":
            return self.me
        if path == f"/repos/{REPO}/issues/{NUMBER}":
            return {"number": NUMBER, "pull_request": {"url": "x"}} if self.is_pr else {"number": NUMBER}
        if path == f"/repos/{REPO}/pulls/{NUMBER}":
            assert self.is_pr, "an issue has no pulls/ entry"
            head = self.heads.pop(0) if len(self.heads) > 1 else self.heads[0]
            return {"state": self.state, "base": {"repo": {"full_name": REPO}},
                    "head": {"sha": head, "repo": {"full_name": self.head_repo} if self.head_repo else None}}
        if path == f"/repos/{REPO}/issues/{NUMBER}/labels?per_page=100":
            return [{"name": n} for n in self.labels]
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path, body):
        when = self.call("POST", path)
        self.fail("POST", path, when, "before")
        self.writes.append(("POST", path, body))
        if path.endswith("/labels"):
            self.labels += [n for n in body["labels"] if n not in self.labels]
        self.fail("POST", path, when, "after")
        return {}

    def delete(self, path):
        when = self.call("DELETE", path)
        self.fail("DELETE", path, when, "before")
        self.writes.append(("DELETE", path, None))
        if not any(path.endswith("/labels/" + n) for n in self.labels):
            raise approve_gate.urllib.error.HTTPError(path, 404, "Label does not exist", {}, None)
        self.labels = [n for n in self.labels if not path.endswith("/labels/" + n)]
        self.fail("DELETE", path, when, "after")
        return {}

    def graphql(self, query, variables):
        self.graphql_calls.append((query, variables))
        if isinstance(self.history, Exception):
            raise self.history
        if callable(self.history):
            return self.history(variables)
        node = {"__typename": "IssueComment", "id": "IC_55", "databaseId": 55, "lastEditedAt": None,
                "editor": None, "includesCreatedEdit": False, "userContentEdits": {"totalCount": 0}}
        node.update(self.history or {})
        return {"nodes": [{k: v for k, v in node.items() if v is not DROP}]}

    def comments(self):
        return [body["body"] for method, path, body in self.writes if path.endswith("/comments")]


def event(action="created", number=NUMBER, comment_id=55):
    return {"action": action, "issue": {"number": number}, "comment": {"id": comment_id}}


def run(github, name="issue_comment", payload=None):
    return approve_gate.run(github, name, payload or event(), REPO, OWNER, OWNER_ID)


class Acceptance(unittest.TestCase):
    def test_owner_comment_at_the_head_labels_then_records(self):
        github = FakeGitHub()
        code, message = run(github)
        self.assertEqual(code, 0, message)
        issue = f"/repos/{REPO}/issues/{NUMBER}"
        self.assertEqual([(m, p) for m, p, _ in github.writes],
                         [("POST", f"{issue}/labels"), ("POST", f"{issue}/comments")])
        self.assertEqual(github.writes[0][2], {"labels": [LABEL]})
        [record] = github.comments()
        self.assertEqual(RECORD.findall(record), [(LABEL, HEAD)])
        self.assertIn(LABEL, github.labels)

    def test_trailing_whitespace_is_not_extra_text(self):
        """The job's if: needs the body to start with `/approve-gate `; the editor may add a trailing newline."""
        self.assertEqual(run(FakeGitHub(body=f"/approve-gate {HEAD}\r\n"))[0], 0)

    def test_reapproval_removes_the_old_label_first_so_github_dates_a_new_one(self):
        """Adding a label already on the PR records no event; the guard would date the old one, before the push."""
        github = FakeGitHub(labels=["ready", LABEL])
        self.assertEqual(run(github)[0], 0)
        issue = f"/repos/{REPO}/issues/{NUMBER}"
        self.assertEqual([(m, p) for m, p, _ in github.writes],
                         [("DELETE", f"{issue}/labels/{LABEL}"), ("POST", f"{issue}/labels"),
                          ("POST", f"{issue}/comments")])

    def test_record_matches_what_ao_ready_reads(self):
        """agent-ops owner_label.MARK: `<!-- owner-label (\\S+) head=([0-9a-f]{40}) -->`."""
        record = approve_gate.head_record(HEAD, "https://example.invalid/c")
        self.assertIn(f"<!-- owner-label {LABEL} head={HEAD} -->", record)
        self.assertFalse(record.lstrip().startswith("/approve-gate"), "the record must not trigger another run")

class GuardCountsTheRecord(unittest.TestCase):
    """The label and record a run writes, judged by gate_guard.owner_label as .github #63 left it.

    Since #63 every gate-change-approved label needs the owner's newest head record posted at or after it to name
    the head, and the record counts only when GraphQL's edit history of that comment, every field of exactly its
    type, shows no edit. The guard's own fixtures hard-code the record text, so only this catches head_record()
    drifting from what the guard reads."""

    LABELED, POSTED, PUSHED = "2026-10-27T12:00:05Z", "2026-10-27T12:00:06Z", "2026-10-27T11:00:00Z"

    def setUp(self):
        import gate_guard
        self.guard = gate_guard
        self.github = FakeGitHub()
        code, message = run(self.github)
        self.assertEqual(code, 0, message)
        [record] = self.github.comments()
        owner = {"login": OWNER, "id": OWNER_ID, "type": "User"}
        # The run's comments as the guard's REST read returns them: the owner's command, then the record.
        self.comments = [dict(self.github.comment, node_id="IC_55"),
                         {"id": 900, "node_id": "IC_900", "user": owner, "body": record,
                          "created_at": self.POSTED, "updated_at": self.POSTED}]
        self.events = [{"id": 1, "event": "labeled", "label": {"name": LABEL}, "actor": {"login": OWNER},
                        "created_at": self.LABELED}]

    def node(self, **change):
        node = {"__typename": "IssueComment", "id": "IC_900", "databaseId": 900, "lastEditedAt": None,
                "editor": None, "includesCreatedEdit": False, "userContentEdits": {"totalCount": 0}}
        node.update(change)
        return node

    def judge(self, node, sha=HEAD):
        asked = []

        class Graphql:
            def graphql(self, query, variables):
                asked.append(variables["ids"])
                return {"nodes": [node]}

        comments = self.guard.with_edit_history(Graphql(), self.comments, OWNER)
        self.assertEqual(asked, [["IC_900"]], "the guard looks up the record, and only the record")
        return self.guard.owner_label(LABEL, {LABEL}, self.events, OWNER, sha,
                                      self.guard.parse_time(self.PUSHED), comments)

    def test_the_record_counts_at_the_head(self):
        counts, reason = self.judge(self.node())
        self.assertTrue(counts, reason)
        self.assertIn(f"names {HEAD[:7]}", reason)

    def test_the_record_does_not_count_for_another_head(self):
        counts, reason = self.judge(self.node(), sha=NEWER)
        self.assertFalse(counts, reason)
        self.assertIn(f"names {HEAD[:7]}, not the head {NEWER[:7]}", reason)

    def test_an_edited_record_does_not_count(self):
        for change in ({"lastEditedAt": self.POSTED}, {"editor": {"login": "someone"}},
                       {"includesCreatedEdit": True}, {"userContentEdits": {"totalCount": 1}}):
            with self.subTest(change=change):
                counts, reason = self.judge(self.node(**change))
                self.assertFalse(counts, reason)
                self.assertIn("no unedited head record", reason)

    def test_a_malformed_edit_history_fails_closed(self):
        for change in ({"databaseId": True}, {"databaseId": 900.0}, {"id": "IC_55"}, {"__typename": "Issue"},
                       {"userContentEdits": {"totalCount": False}}, {"includesCreatedEdit": 0}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.judge(self.node(**change))


class Rejections(unittest.TestCase):
    def assert_refused(self, github, why, replied=True, payload=None, name="issue_comment"):
        code, message = run(github, name, payload)
        self.assertEqual(code, 1, message)
        self.assertIn(why, message)
        self.assertNotIn(LABEL, github.labels)
        self.assertFalse([w for w in github.writes if w[1].endswith("/labels")], "touched labels")
        self.assertFalse([b for b in github.comments() if RECORD.search(b)], "posted a head record")
        self.assertEqual(len(github.comments()), 1 if replied else 0)
        if replied:
            self.assertIn("was not applied", github.comments()[0])

    def test_wrong_author(self):
        self.assert_refused(FakeGitHub(login="dflippojr-workers[bot]", uid=111, kind="Bot"), "not the owner",
                            replied=False)

    def test_look_alike_login(self):
        for login, uid in (("dflippojr-", 222), ("dfIippojr", 333), ("DFLIPPOJR", 444), ("dflippojr ", 555)):
            with self.subTest(login=login):
                self.assert_refused(FakeGitHub(login=login, uid=uid), "not the owner", replied=False)

    def test_owner_login_with_another_id(self):
        """A login is reusable after a rename; the id is not."""
        self.assert_refused(FakeGitHub(uid=OWNER_ID + 1), "not the owner", replied=False)

    def test_owner_id_with_another_login_or_type(self):
        self.assert_refused(FakeGitHub(login="someone-else"), "not the owner")
        self.assert_refused(FakeGitHub(kind="Bot"), "not the owner")

    def test_edited_comment(self):
        self.assert_refused(FakeGitHub(updated="2026-10-27T12:00:05Z"), "edited")

    def test_edited_event(self):
        self.assert_refused(FakeGitHub(), "only issue_comment created", replied=False, payload=event("edited"))

    def test_other_events(self):
        self.assert_refused(FakeGitHub(), "only issue_comment created", replied=False, name="pull_request_target")

    def test_stale_sha(self):
        self.assert_refused(FakeGitHub(body=f"/approve-gate {HEAD}", heads=(NEWER,)), f"head is now {NEWER}")

    def test_short_sha(self):
        for sha in (HEAD[:7], HEAD[:12], HEAD[:39]):
            with self.subTest(sha=sha):
                self.assert_refused(FakeGitHub(body=f"/approve-gate {sha}"), "exactly `/approve-gate <sha>`")

    def test_extra_text(self):
        for body in (f"/approve-gate {HEAD} looks good", f"LGTM\n/approve-gate {HEAD}", f"/approve-gate  {HEAD}",
                     f"/approve-gate {HEAD.upper()}", f"/Approve-Gate {HEAD}", f"/approve-gate {HEAD}0",
                     f"/approve-gate\t{HEAD}", f"/approve-gate {HEAD}\n\nthanks", "/approve-gate"):
            with self.subTest(body=body):
                self.assert_refused(FakeGitHub(body=body), "exactly `/approve-gate <sha>`")

    def test_comment_on_an_issue(self):
        self.assert_refused(FakeGitHub(is_pr=False), "not a pull request")

    def test_fork_pull_request(self):
        self.assert_refused(FakeGitHub(head_repo="someone/demo"), "fork pull requests")

    def test_deleted_fork(self):
        self.assert_refused(FakeGitHub(head_repo=None), "a deleted repository")

    def test_closed_pull_request(self):
        self.assert_refused(FakeGitHub(state="closed"), "not open")

    def test_comment_from_another_pull_request(self):
        github = FakeGitHub()
        github.comment["issue_url"] = f"https://api.github.com/repos/{REPO}/issues/8"
        self.assert_refused(github, "is not on", replied=False)

    def test_token_of_someone_else_writes_nothing(self):
        github = FakeGitHub(token_login="dflippojr-workers[bot]", token_id=999)
        code, message = run(github)
        self.assertEqual(code, 2)
        self.assertIn("OWNER_GATE_TOKEN belongs to", message)
        self.assertEqual(github.writes, [])


class HeadMovesDuringApply(unittest.TestCase):
    def test_push_between_check_and_label_removes_the_label(self):
        """The guard dates the label against the newest push; a push before the label would be covered."""
        github = FakeGitHub(heads=(HEAD, NEWER))
        code, message = run(github)
        self.assertEqual(code, 1)
        self.assertIn(f"moved to {NEWER}", message)
        self.assertNotIn(LABEL, github.labels)
        self.assertEqual(github.writes[-2][0], "DELETE")
        self.assertIn("removed again", github.comments()[-1])


ISSUE = f"/repos/{REPO}/issues/{NUMBER}"
PULL = f"/repos/{REPO}/pulls/{NUMBER}"
LABEL_POST, RECORD_POST = ("POST", f"{ISSUE}/labels"), ("POST", f"{ISSUE}/comments")
HEAD_GET, LABEL_DELETE = ("GET", PULL), ("DELETE", f"{ISSUE}/labels/{LABEL}")


class FailureAfterTheLabel(unittest.TestCase):
    """Review of f8baa01, finding 1: once the label is on, every way the run can fail takes it off again. The gate
    guard dates the label against the newest push, so a label left on after a push between the check and the
    label approves a head the owner never named."""

    FAILURES = {
        "the label POST, applied but the response lost": {LABEL_POST: {1: "after"}},
        "the label POST, not applied": {LABEL_POST: {1: "before"}},
        "the record POST": {RECORD_POST: {1: "before"}},
        "the record POST, posted but the response lost": {RECORD_POST: {1: "after"}},
        "the final head GET": {HEAD_GET: {2: "before"}},
    }

    def test_each_failure_removes_the_label(self):
        for pushed in (False, True):
            for failure, faults in self.FAILURES.items():
                for labels in ((), (LABEL,)):
                    with self.subTest(failure=failure, push_before_label=pushed, already_labeled=bool(labels)):
                        github = FakeGitHub(heads=(HEAD, NEWER) if pushed else (HEAD,), faults=faults,
                                            labels=labels)
                        with self.assertRaises(approve_gate.urllib.error.URLError):
                            run(github)
                        self.assertNotIn(LABEL, github.labels)

    def test_head_moved_after_a_push_between_check_and_label(self):
        github = FakeGitHub(heads=(HEAD, NEWER), labels=(LABEL,))
        code, message = run(github)
        self.assertEqual(code, 1, message)
        self.assertNotIn(LABEL, github.labels)

    def test_a_failure_after_the_label_exits_2_with_the_label_off(self):
        for failure, faults in self.FAILURES.items():
            with self.subTest(failure=failure):
                github = FakeGitHub(heads=(HEAD, NEWER), faults=faults)
                code, out = main_with(self, github)
                self.assertEqual(code, 2, out)
                self.assertIn("::error::could not read or write", out)
                self.assertNotIn(LABEL, github.labels)

    def test_a_failed_removal_is_loud_and_exits_2(self):
        stuck = {LABEL_DELETE: {1: "before"}}
        failures = {k: v for k, v in self.FAILURES.items() if k != "the label POST, not applied"}
        for failure, faults in list(failures.items()) + [("the head moved", {})]:
            with self.subTest(failure=failure):
                github = FakeGitHub(heads=(HEAD, NEWER), faults={**faults, **stuck})
                code, out = main_with(self, github)
                self.assertEqual(code, 2, out)
                self.assertIn(LABEL, github.labels, "the fake's label really is still on")
                self.assertRegex(out, rf"(?m)^::error::{LABEL} is still on {re.escape(REPO)}#{NUMBER}")
                self.assertIn("remove it by hand", out)


def fail_call(github, method, suffix, error, nth=1, acted=False):
    """The nth call to method ("get", "post" or "delete") on a path ending in suffix raises error; with acted,
    GitHub does the write first and only the response is lost."""
    real = getattr(github, method)
    seen = []

    def call(path, *args):
        if path.endswith(suffix):
            seen.append(path)
            if len(seen) == nth:
                if acted:
                    real(path, *args)
                raise error
        return real(path, *args)

    setattr(github, method, call)


class HttpExceptions(unittest.TestCase):
    """Issue #47: an http.client.HTTPException that is not an OSError (IncompleteRead, BadStatusLine) escaped main() as
    a traceback and exit 1. RemoteDisconnected is an OSError, so main() already reported it."""

    def test_remote_disconnected_on_the_record_post_exits_2_and_removes_the_label(self):
        github = FakeGitHub()
        fail_call(github, "post", "/comments", http.client.RemoteDisconnected("Remote end closed connection"))
        code, out = main_with(self, github)
        self.assertEqual(code, 2, out)
        self.assertIn("::error::could not read or write the pull request", out)
        self.assertNotIn(LABEL, github.labels)
        self.assertIn(("DELETE", f"/repos/{REPO}/issues/{NUMBER}/labels/{LABEL}", None), github.writes)

    def test_other_http_exceptions_exit_2_with_the_label_off(self):
        points = [("the comment GET", "get", "/issues/comments/55", 1, False),
                  ("the label POST, applied but the response lost", "post", "/labels", 1, True),
                  ("the record POST", "post", "/comments", 1, False),
                  ("the record POST, posted but the response lost", "post", "/comments", 1, True),
                  ("the final head GET", "get", f"/pulls/{NUMBER}", 2, False)]
        for point, method, suffix, nth, acted in points:
            for error in (http.client.IncompleteRead(b""), http.client.BadStatusLine("")):
                with self.subTest(point=point, error=type(error).__name__):
                    github = FakeGitHub()
                    fail_call(github, method, suffix, error, nth, acted)
                    code, out = main_with(self, github)
                    self.assertEqual(code, 2, out)
                    self.assertIn(f"::error::could not read or write the pull request ({error})", out)
                    self.assertNotIn(LABEL, github.labels)


def main_with(test, github):
    """approve_gate.main() against github, with the workflow's environment; (exit code, stdout)."""
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    event_path = pathlib.Path(tmp.name, "event.json")
    event_path.write_text(json.dumps(event()), encoding="utf-8")
    env = {"OWNER_GATE_TOKEN": "t", "GITHUB_REPOSITORY": REPO, "GITHUB_REPOSITORY_OWNER": OWNER,
           "GITHUB_REPOSITORY_OWNER_ID": str(OWNER_ID), "GITHUB_EVENT_NAME": "issue_comment",
           "GITHUB_EVENT_PATH": str(event_path)}
    out = io.StringIO()
    with mock.patch.dict(os.environ, env, clear=True), \
            mock.patch.object(approve_gate, "GitHub", return_value=github), redirect_stdout(out):
        return approve_gate.main(), out.getvalue()


class CommandEditHistory(unittest.TestCase):
    """REST's one-second timestamps can't show an edit in the second the command was posted (.github #63), so the
    command counts only when GraphQL's edit history, every field of exactly its type, shows it never edited. Any
    edit, malformed answer or unreadable history applies nothing: exit 2 and a reply naming why."""

    NODE = {"__typename": "IssueComment", "id": "IC_55", "databaseId": 55, "lastEditedAt": None,
            "editor": None, "includesCreatedEdit": False, "userContentEdits": {"totalCount": 0}}

    def assert_not_applied(self, github, why):
        code, message = run(github)
        self.assertEqual(code, 2, message)
        self.assertIn(why, message)
        self.assertNotIn(LABEL, github.labels)
        self.assertEqual([(m, p) for m, p, _ in github.writes], [("POST", f"/repos/{REPO}/issues/{NUMBER}/comments")],
                         "only the reply is written")
        [reply] = github.comments()
        self.assertTrue(reply.startswith(f"`{LABEL}` was not applied: "), reply)
        self.assertIn(why, reply)
        self.assertIsNone(RECORD.search(reply))

    def test_queries_the_command_by_its_node_id(self):
        github = FakeGitHub()
        self.assertEqual(run(github)[0], 0)
        self.assertEqual(github.graphql_calls, [(approve_gate.EDIT_QUERY, {"ids": ["IC_55"]})])

    def test_edited_in_the_second_it_was_posted(self):
        """The REST timestamps are equal (FakeGitHub's default); only GraphQL shows the edit."""
        for change in ({"lastEditedAt": CREATED}, {"editor": {"login": "someone"}}, {"includesCreatedEdit": True},
                       {"userContentEdits": {"totalCount": 1}}):
            with self.subTest(change=change):
                self.assert_not_applied(FakeGitHub(history=change), "edit history shows the comment was edited")

    def test_a_missing_key(self):
        for key in self.NODE:
            with self.subTest(key=key):
                self.assert_not_applied(FakeGitHub(history={key: DROP}), "is malformed")

    def test_a_key_of_the_wrong_type_or_value(self):
        for change in ({"databaseId": True}, {"databaseId": 55.0}, {"databaseId": "55"}, {"databaseId": 56},
                       {"id": "IC_56"}, {"id": None}, {"__typename": "PullRequestReviewComment"},
                       {"lastEditedAt": 0}, {"editor": "someone"}, {"includesCreatedEdit": 0},
                       {"includesCreatedEdit": None}, {"userContentEdits": None}, {"userContentEdits": {}},
                       {"userContentEdits": {"totalCount": -1}}, {"userContentEdits": {"totalCount": True}},
                       {"userContentEdits": {"totalCount": 0.0}}):
            with self.subTest(change=change):
                self.assert_not_applied(FakeGitHub(history=change), "is malformed")

    def test_a_node_that_is_not_an_object(self):
        for node in (None, [], "IC_55"):
            with self.subTest(node=node):
                self.assert_not_applied(FakeGitHub(history=lambda v, node=node: {"nodes": [node]}), "is malformed")

    def test_an_unreadable_history(self):
        for history in (approve_gate.urllib.error.URLError("down"),
                        approve_gate.urllib.error.HTTPError("x", 403, "Resource not accessible", {}, None),
                        ValueError("the GraphQL API answered with errors"),
                        lambda v: None, lambda v: {}, lambda v: {"nodes": None}, lambda v: {"nodes": []},
                        lambda v: {"nodes": [dict(self.NODE), dict(self.NODE)]}):
            with self.subTest(history=history):
                self.assert_not_applied(FakeGitHub(history=history), "edit history could not be read")

    def test_a_comment_without_a_node_id(self):
        for node_id in (None, "", 55):
            with self.subTest(node_id=node_id):
                github = FakeGitHub(node_id=node_id)
                self.assert_not_applied(github, "edit history could not be read")
                self.assertEqual(github.graphql_calls, [])

    def test_other_refusals_come_first_and_read_no_history(self):
        """Someone else's comment, a REST-visible edit or another body is refused (exit 1) before any query."""
        for github in (FakeGitHub(login="someone-else", uid=1), FakeGitHub(updated="2026-10-27T12:00:05Z"),
                       FakeGitHub(body="/approve-gate abc")):
            with self.subTest(comment=github.comment):
                self.assertEqual(run(github)[0], 1)
                self.assertEqual(github.graphql_calls, [])

    def test_a_token_of_someone_else_writes_nothing(self):
        github = FakeGitHub(history={"editor": {"login": "someone"}}, token_login="x", token_id=9)
        code, message = run(github)
        self.assertEqual(code, 2)
        self.assertIn("OWNER_GATE_TOKEN belongs to @x", message)
        self.assertEqual(github.writes, [])


class EditCheckIsTheGuards(unittest.TestCase):
    """The workflow's sparse checkout carries only approve_gate.py, so it holds a copy of the gate guard's
    edit-history check (.github #63). The copy must stay the guard's, byte for byte."""

    def test_same_source(self):
        import inspect
        import gate_guard
        for name in ("_check_edit_node", "_never_edited"):
            with self.subTest(name=name):
                self.assertEqual(inspect.getsource(getattr(approve_gate, name)),
                                 inspect.getsource(getattr(gate_guard, name)))
        self.assertEqual(approve_gate.EDIT_QUERY, gate_guard.EDIT_QUERY)
        self.assertEqual(approve_gate.EDIT_KEYS, gate_guard.EDIT_KEYS)


class GraphqlCall(unittest.TestCase):
    """GitHub.graphql: one POST to the GraphQL endpoint; errors, even beside data, raise."""

    def call(self, answer, **env):
        seen = []

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def urlopen(request, timeout):
            seen.append(request)
            return Response(json.dumps(answer).encode("utf-8"))

        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(approve_gate.urllib.request, "urlopen", urlopen):
            try:
                return approve_gate.GitHub("https://api.example", "t").graphql("query", {"ids": ["IC_1"]}), seen
            except ValueError as e:
                return e, seen

    def test_posts_the_query_and_returns_the_data(self):
        data, [request] = self.call({"data": {"nodes": []}})
        self.assertEqual(data, {"nodes": []})
        self.assertEqual((request.get_method(), request.full_url), ("POST", "https://api.example/graphql"))
        self.assertEqual(json.loads(request.data), {"query": "query", "variables": {"ids": ["IC_1"]}})
        self.assertEqual(request.get_header("Authorization"), "Bearer t")

    def test_uses_the_runner_graphql_url(self):
        _, [request] = self.call({"data": {}}, GITHUB_GRAPHQL_URL="https://ghe.example/api/graphql")
        self.assertEqual(request.full_url, "https://ghe.example/api/graphql")

    def test_errors_raise(self):
        for answer in ({"errors": [{"message": "no"}], "data": {"nodes": []}}, {"data": None}, [], None):
            with self.subTest(answer=answer):
                self.assertIsInstance(self.call(answer)[0], ValueError)


class Main(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.event_path = pathlib.Path(tmp.name, "event.json")
        self.event_path.write_text(json.dumps(event()), encoding="utf-8")
        self.summary = pathlib.Path(tmp.name, "summary.md")
        self.env = {"OWNER_GATE_TOKEN": "t", "GITHUB_REPOSITORY": REPO, "GITHUB_REPOSITORY_OWNER": OWNER,
                    "GITHUB_REPOSITORY_OWNER_ID": str(OWNER_ID), "GITHUB_EVENT_NAME": "issue_comment",
                    "GITHUB_EVENT_PATH": str(self.event_path), "GITHUB_STEP_SUMMARY": str(self.summary)}

    def main(self, github, **env):
        out = io.StringIO()
        with mock.patch.dict(os.environ, dict(self.env, **env), clear=True), \
                mock.patch.object(approve_gate, "GitHub", return_value=github), redirect_stdout(out):
            return approve_gate.main(), out.getvalue()

    def test_applies_and_reports(self):
        code, out = self.main(FakeGitHub())
        self.assertEqual(code, 0)
        self.assertIn("::notice::applied gate-change-approved", out)
        self.assertIn("applied", self.summary.read_text(encoding="utf-8"))

    def test_missing_token_writes_nothing(self):
        github = FakeGitHub()
        code, out = self.main(github, OWNER_GATE_TOKEN="")
        self.assertEqual(code, 2)
        self.assertIn("OWNER_GATE_TOKEN is not set", out)
        self.assertEqual(github.writes, [])

    def test_api_error_is_an_error(self):
        github = FakeGitHub()
        github.get = mock.Mock(side_effect=approve_gate.urllib.error.URLError("down"))
        code, out = self.main(github)
        self.assertEqual(code, 2)
        self.assertIn("::error::could not read or write", out)

    def test_an_edited_command_exits_2(self):
        github = FakeGitHub(history={"userContentEdits": {"totalCount": 1}})
        code, out = self.main(github)
        self.assertEqual(code, 2)
        self.assertIn("::error::", out)
        self.assertNotIn(LABEL, github.labels)

    def test_refusal_cannot_issue_workflow_commands(self):
        github = FakeGitHub(login="x\n::notice::forged", uid=1)
        code, out = self.main(github)
        self.assertEqual(code, 1)
        self.assertEqual([line for line in out.splitlines() if line.startswith("::")], [out.strip()])


class WorkflowSafety(unittest.TestCase):
    """Static rules: default-branch caller, no PR code, a pinned script, the token only where it is used."""

    def text(self, path):
        return path.read_text(encoding="utf-8")

    def test_reusable_workflow(self):
        text = self.text(WORKFLOWS / "approve-gate.yml")
        self.assertRegex(text, r"(?m)^on:\n  workflow_call:")
        self.assertNotIn("github.event.pull_request", text)
        self.assertNotIn("secrets: inherit", text)
        for uses in re.findall(r"(?m)^\s*(?:- )?uses:\s*(\S+)", text):
            self.assertRegex(uses, r"@[0-9a-f]{40}$", f"{uses} is not pinned to a full SHA")
        self.assertEqual(text.count("uses: actions/checkout@"), 1)
        self.assertIn("persist-credentials: false", text)
        self.assertIn("repository: dflippojr/.github", text)
        self.assertIn("sparse-checkout: scripts/approve_gate.py", text)
        self.assertIn("grep -Eqx '[0-9a-f]{40}'", text)
        # The owner's token reaches the one step that runs the script, and nothing else.
        self.assertEqual(text.count("secrets.OWNER_GATE_TOKEN"), 1)
        job = yaml.safe_load(text)["jobs"]["approve"]
        # The token is a secret of the caller's approve-gate environment (default branch only); a job that calls a
        # reusable workflow can't declare an environment, so this called job does.
        self.assertEqual(job["environment"], "approve-gate")
        self.assertRegex(text, r"(?m)^    environment: approve-gate$")
        self.assertEqual(job["runs-on"], "ubuntu-latest", "an owner token never runs on a self-hosted runner")
        self.assertEqual(yaml.safe_load(text)["permissions"], {"contents": "read"})
        step = job["steps"][-1]
        self.assertEqual(step["env"], {"OWNER_GATE_TOKEN": "${{ secrets.OWNER_GATE_TOKEN }}"})
        self.assertEqual(step["run"], "python3 approve-gate/scripts/approve_gate.py")
        for condition in ("github.event.action == 'created'", "github.event.issue.pull_request",
                          "github.event.comment.user.id == github.repository_owner_id",
                          "startsWith(github.event.comment.body, '/approve-gate ')"):
            self.assertIn(condition, job["if"])

    def test_callers(self):
        for path in (WORKFLOWS / "approve-gate-base.yml", TEMPLATE):
            with self.subTest(caller=path.name):
                text = self.text(path)
                data = yaml.safe_load(text)
                self.assertEqual(data[True], {"issue_comment": {"types": ["created"]}})
                self.assertEqual(data["permissions"], {"contents": "read"})
                self.assertNotIn("actions/checkout", text)
                self.assertNotIn("secrets: inherit", text)
                self.assertEqual(data["concurrency"], {"group": "approve-gate-${{ github.event.issue.number }}",
                                                       "cancel-in-progress": False})
                job = data["jobs"]["approve-gate"]
                self.assertEqual(job["secrets"], {"OWNER_GATE_TOKEN": "${{ secrets.OWNER_GATE_TOKEN }}"})
                self.assertNotIn("environment", job, "GitHub refuses environment on a job that calls a workflow")

    def test_only_the_approve_job_reads_the_token_or_declares_its_environment(self):
        """Any default-branch job that declares approve-gate passes its branch rule and gets the token, so the one
        reader is approve-gate.yml's approve job; a caller job may only pass the secret on by name."""
        readers, callers = [], []
        for path in sorted(WORKFLOWS.glob("*.y*ml")) + [TEMPLATE]:
            for name, job in (yaml.safe_load(self.text(path)).get("jobs") or {}).items():
                text = yaml.safe_dump(job)
                if "OWNER_GATE_TOKEN" not in text and "approve-gate" not in str(job.get("environment", "")):
                    continue
                if "uses" in job:
                    self.assertRegex(job["uses"], r"(^\./\.github|^dflippojr/\.github/\.github)/workflows/approve-gate"
                                     r"\.yml(@|$)", f"{path.name}:{name}")
                    self.assertEqual(job["secrets"], {"OWNER_GATE_TOKEN": "${{ secrets.OWNER_GATE_TOKEN }}"})
                    rest = {k: v for k, v in job.items() if k != "secrets"}
                    self.assertNotIn("OWNER_GATE_TOKEN", yaml.safe_dump(rest), f"{path.name}:{name}")
                    callers.append(path.name)
                else:
                    readers.append((path.name, name, job.get("environment")))
        self.assertEqual(readers, [("approve-gate.yml", "approve", "approve-gate")])
        self.assertEqual(callers, ["approve-gate-base.yml", "approve-gate-caller.yml"])

    def test_template_pins_one_sha_in_both_places(self):
        text = self.text(TEMPLATE)
        self.assertIn("uses: dflippojr/.github/.github/workflows/approve-gate.yml@<full-sha>", text)
        self.assertIn("approve-ref: <full-sha>", text)


if __name__ == "__main__":
    unittest.main()
