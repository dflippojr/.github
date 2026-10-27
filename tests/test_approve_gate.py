"""approve_gate.py: only the owner's own unedited `/approve-gate <head sha>` on a same-repo PR labels it."""
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


class FakeGitHub:
    """Answers approve_gate's API calls from a small model of one pull request, and logs every write.

    heads: the PR head each successive GET of the pull request returns (the last one repeats), so a push
    between the check and the label can be simulated. faults: (method, path) -> {n: when}, the nth such call
    raises URLError; a write with when "after" takes effect first (GitHub did it, the response was lost), one with
    "before" does not. Removing a label the issue does not have is a 404, as on GitHub.
    """

    def __init__(self, body=None, login=OWNER, uid=OWNER_ID, updated=CREATED, is_pr=True, head_repo=REPO,
                 state="open", labels=(), heads=(HEAD,), token_login=OWNER, token_id=OWNER_ID, kind="User",
                 faults=None):
        self.comment = {"id": 55, "user": {"login": login, "id": uid, "type": kind},
                        "body": f"/approve-gate {HEAD}" if body is None else body,
                        "created_at": CREATED, "updated_at": updated,
                        "issue_url": f"https://api.github.com/repos/{REPO}/issues/{NUMBER}",
                        "html_url": f"https://github.com/{REPO}/pull/{NUMBER}#issuecomment-55"}
        self.is_pr, self.head_repo, self.state = is_pr, head_repo, state
        self.labels, self.heads = list(labels), list(heads)
        self.me = {"login": token_login, "id": token_id}
        self.writes = []
        self.faults, self.calls = faults or {}, {}

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
