"""The pull_request_target gate guard: owner labels dated against the head push, verdicts posted on the head."""
import io
import json
import os
import pathlib
import re
import subprocess
import tempfile
import unittest
from unittest import mock

from test_gate_guard import FIXTURES, _trees, clean_env, gate_guard

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCENARIOS = pathlib.Path(__file__).resolve().parent / "fixtures" / "gate_guard_target"
WORKFLOWS = ROOT / ".github" / "workflows"
OWNER, REPO, NUMBER, HEAD_REF = "dflippojr", "dflippojr/demo", 7, "feature"


class FakeGitHub:
    """Answers the API calls of run_target from a scenario and records the check runs it posts."""

    def __init__(self, merge_base, head, scenario):
        self.merge_base, self.head, self.scenario = merge_base, head, scenario
        self.posted = []

    def get(self, path):
        assert path == f"/repos/{REPO}/compare/main...{self.head}?per_page=1", path
        return {"merge_base_commit": {"sha": self.merge_base}}

    def pages(self, path):
        s = self.scenario
        if path == f"/repos/{REPO}/issues/{NUMBER}/labels":
            return [{"name": n} for n in s["labels"]]
        if path == f"/repos/{REPO}/issues/{NUMBER}/events":
            return [{"id": i, "event": e["event"], "label": {"name": e["label"]}, "actor": {"login": e["actor"]},
                     "created_at": e["created_at"]} for i, e in enumerate(s["events"])]
        if path == f"/repos/{REPO}/activity?ref=refs/heads/{HEAD_REF}":
            # Newest first, as GitHub returns it; pushes of other commits and refs are noise.
            noise = [{"after": "f" * 40, "ref": f"refs/heads/{HEAD_REF}", "timestamp": "2026-10-16T23:00:00Z"},
                     {"after": self.head, "ref": "refs/heads/other", "timestamp": "2026-10-16T23:00:00Z"}]
            push = [{"after": self.head, "ref": f"refs/heads/{HEAD_REF}", "timestamp": s["pushed_at"]}]
            return noise + (push if s["pushed_at"] else [])
        raise AssertionError(f"unexpected API path {path}")

    def post(self, path, body):
        assert path == f"/repos/{REPO}/check-runs", path
        self.posted.append(body)
        return {"id": 1}


def git(root, *args):
    out = subprocess.run(["git", "-C", str(root), "-c", "core.autocrlf=false", *args], check=True,
                         capture_output=True)
    return out.stdout.decode().strip()


def commit_tree(root, files, message):
    for p in root.iterdir():
        if p.name != ".git":
            subprocess.run(["git", "-C", str(root), "rm", "-rqf", "--ignore-unmatch", p.name], check=True,
                           capture_output=True)
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))
    git(root, "add", "-A")
    git(root, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", message, "--allow-empty")
    return git(root, "rev-parse", "HEAD")


def make_remote(tmp, diff_name):
    """A repository at <tmp>/server/<REPO>.git holding the fixture's base and head commits."""
    remote = pathlib.Path(tmp, "server", f"{REPO}.git")
    remote.mkdir(parents=True)
    git(remote, "init", "-q")
    git(remote, "config", "uploadpack.allowAnySHA1InWant", "true")
    before, after = _trees((FIXTURES / f"{diff_name}.diff").read_text(encoding="utf-8"))
    base = commit_tree(remote, before, "base")
    head = commit_tree(remote, after, "head")
    return pathlib.Path(tmp, "server").as_uri(), base, head


def run_scenario(scenario, tmp):
    """(exit code, stdout, fake API, head sha, outputs) of the CLI on a scenario."""
    server, base, head = make_remote(tmp, scenario["diff"])
    event = pathlib.Path(tmp, "event.json")
    event.write_text(json.dumps({"pull_request": {
        "number": NUMBER, "head": {"sha": head, "ref": HEAD_REF, "repo": {"full_name": REPO}},
        "base": {"ref": "main", "sha": base}}}), encoding="utf-8")
    outputs = pathlib.Path(tmp, "outputs.txt")
    env = clean_env({"GITHUB_EVENT_PATH": str(event), "GITHUB_REPOSITORY": REPO, "GITHUB_REPOSITORY_OWNER": OWNER,
                     "GITHUB_SERVER_URL": server, "GITHUB_OUTPUT": str(outputs), "GITHUB_RUN_ID": "99"})
    fake = FakeGitHub(base, head, scenario)
    out = io.StringIO()
    with mock.patch.dict(os.environ, env, clear=True), mock.patch("sys.stdout", out), \
            mock.patch.object(gate_guard, "GitHub", return_value=fake):
        code = gate_guard.main(["--pull-request-target", "--guard-sha", "a" * 40])
    return code, out.getvalue(), fake, head, outputs.read_text(encoding="utf-8")


class Scenarios(unittest.TestCase):
    """Each fixture in tests/fixtures/gate_guard_target runs end to end through git and a fake API."""

    def test_issue_cases_have_fixtures(self):
        names = {p.stem for p in SCENARIOS.glob("*.json")}
        self.assertLessEqual({"head-edits-caller", "stale-owner-label", "bot-label", "owner-label-after-head"}, names)

    def test_scenarios(self):
        for path in sorted(SCENARIOS.glob("*.json")):
            scenario = json.loads(path.read_text(encoding="utf-8"))
            with self.subTest(scenario=path.stem), tempfile.TemporaryDirectory() as tmp:
                code, out, fake, head, outputs = run_scenario(scenario, tmp)
                expect = scenario["expect"]
                self.assertEqual(code, expect["exit"], out)
                [check] = fake.posted
                self.assertEqual(check["name"], "gate-guard")
                self.assertEqual(check["head_sha"], head)
                self.assertEqual(check["conclusion"], expect["conclusion"])
                self.assertEqual(check["output"]["title"], expect["title"])
                self.assertIn(expect["reason"].format(head=head[:7]), check["output"]["summary"])
                self.assertTrue(check["details_url"].endswith(f"/{REPO}/actions/runs/99"))
                self.assertIn(f"conclusion={expect['conclusion']}", outputs)

    def test_head_edits_caller_is_judged_by_the_base(self):
        """The head's caller would post a passing check; the default branch's guard posts a failing one."""
        scenario = json.loads((SCENARIOS / "head-edits-caller.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            code, _, fake, _, _ = run_scenario(scenario, tmp)
        self.assertEqual(code, 1)
        summary = fake.posted[0]["output"]["summary"]
        self.assertIn("| gate-config | `.github/workflows/gate-guard-target.yml` | workflow modified |", summary)
        self.assertIn("removed-tests | `tests/test_billing.py`", summary)
        self.assertIn("none of them run", summary)


class OwnerLabel(unittest.TestCase):
    PUSHED = gate_guard.parse_time("2026-10-16T12:00:00Z")

    @staticmethod
    def event(event, actor, at, label=gate_guard.APPROVAL_LABEL, i=0):
        return {"id": i, "event": event, "label": {"name": label}, "actor": {"login": actor}, "created_at": at}

    def verdict(self, events, labels=(gate_guard.APPROVAL_LABEL,), pushed=PUSHED):
        return gate_guard.owner_label(gate_guard.APPROVAL_LABEL, set(labels), events, "Owner", "c" * 40, pushed)

    def test_label_not_on_the_pull_request(self):
        self.assertEqual(self.verdict([], labels=()), (False, "`gate-change-approved` is not on this pull request"))

    def test_no_event_for_a_present_label(self):
        counts, why = self.verdict([self.event("labeled", "owner", "2026-10-16T13:00:00Z", label="ready")])
        self.assertFalse(counts)
        self.assertIn("no labeled event", why)

    def test_owner_login_matches_case_insensitively(self):
        counts, _ = self.verdict([self.event("labeled", "owner", "2026-10-16T13:00:00Z")])
        self.assertTrue(counts)

    def test_same_second_as_the_push_is_stale(self):
        counts, why = self.verdict([self.event("labeled", "owner", "2026-10-16T12:00:00Z")])
        self.assertFalse(counts)
        self.assertIn("before ccccccc was pushed", why)

    def test_newest_event_wins_by_time_then_id(self):
        events = [self.event("labeled", "owner", "2026-10-16T13:00:00Z", i=2),
                  self.event("unlabeled", "owner", "2026-10-16T13:00:00Z", i=1)]
        self.assertTrue(self.verdict(events)[0])
        events.append(self.event("unlabeled", "bot", "2026-10-16T13:00:00Z", i=3))
        self.assertEqual(self.verdict(events), (False, "`gate-change-approved` was last removed, by @bot"))

    def test_unparseable_label_time_fails_closed(self):
        self.assertFalse(self.verdict([self.event("labeled", "owner", "yesterday")])[0])


class HeadPushedAt(unittest.TestCase):
    def test_newest_push_of_the_sha_to_the_ref(self):
        sha, ref = "a" * 40, "refs/heads/x"
        activity = [{"after": sha, "ref": ref, "timestamp": "2026-10-16T09:00:00Z"},
                    {"after": sha, "ref": ref, "timestamp": "2026-10-16T11:00:00Z"},
                    {"after": sha, "ref": "refs/heads/y", "timestamp": "2026-10-16T12:00:00Z"},
                    {"after": "b" * 40, "ref": ref, "timestamp": "2026-10-16T13:00:00Z"}]
        self.assertEqual(gate_guard.head_pushed_at(activity, ref, sha), gate_guard.parse_time("2026-10-16T11:00:00Z"))
        self.assertIsNone(gate_guard.head_pushed_at(activity, ref, "c" * 40))


class TargetFailures(unittest.TestCase):
    def run_target(self, fake, event):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp, "event.json")
            path.write_text(json.dumps(event), encoding="utf-8")
            env = clean_env({"GITHUB_EVENT_PATH": str(path), "GITHUB_REPOSITORY": REPO})
            with mock.patch.dict(os.environ, env, clear=True), mock.patch("sys.stdout", io.StringIO()), \
                    mock.patch("sys.stderr", io.StringIO()) as err:
                code = gate_guard.run_target(gate_guard.parse_args(["--pull-request-target"]), fake)
        return code, err.getvalue()

    EVENT = {"pull_request": {"number": NUMBER, "head": {"sha": "d" * 40, "ref": HEAD_REF, "repo": None},
                              "base": {"ref": "main"}}}

    def test_needs_a_pull_request_event(self):
        code, err = self.run_target(mock.Mock(), {})
        self.assertEqual(code, 2)
        self.assertIn("needs a pull_request_target event", err)

    def test_unreadable_diff_posts_a_failure(self):
        fake = mock.Mock()
        fake.get.side_effect = gate_guard.urllib.error.URLError("offline")
        fake.pages.return_value = []
        code, _ = self.run_target(fake, self.EVENT)
        self.assertEqual(code, 2)
        body = fake.post.call_args[0][1]
        self.assertEqual((body["conclusion"], body["output"]["title"]), ("failure", "Could not judge this pull request"))
        self.assertIn("could not read the pull request's diff", body["output"]["summary"])

    def test_unreadable_labels_post_a_failure(self):
        fake = mock.Mock()
        fake.get.return_value = {"merge_base_commit": {"sha": "e" * 40}}
        fake.pages.side_effect = gate_guard.urllib.error.URLError("rate limited")
        with mock.patch.object(gate_guard, "fetch_diff", return_value=""):
            code, _ = self.run_target(fake, self.EVENT)
        self.assertEqual(code, 2)
        self.assertIn("could not read the pull request's labels", fake.post.call_args[0][1]["output"]["summary"])

    def test_deleted_fork_cannot_date_the_label(self):
        fake = mock.Mock()
        fake.get.return_value = {"merge_base_commit": {"sha": "e" * 40}}
        fake.pages.side_effect = lambda path: (
            [{"name": "findings-accepted"}] if path.endswith("/labels") else
            [{"id": 1, "event": "labeled", "label": {"name": "findings-accepted"}, "actor": {"login": "dflippojr"},
              "created_at": "2026-10-16T13:00:00Z"}])
        with mock.patch.object(gate_guard, "fetch_diff", return_value=""):
            code, _ = self.run_target(fake, self.EVENT)
        self.assertEqual(code, 1)
        self.assertIn("GitHub has no record of when", fake.post.call_args[0][1]["output"]["summary"])

    def test_check_run_post_failure_exits_2(self):
        fake = mock.Mock()
        fake.get.return_value = {"merge_base_commit": {"sha": "e" * 40}}
        fake.pages.return_value = []
        fake.post.side_effect = gate_guard.urllib.error.URLError("forbidden")
        with mock.patch.object(gate_guard, "fetch_diff", return_value=""):
            code, err = self.run_target(fake, self.EVENT)
        self.assertEqual(code, 2)
        self.assertIn("could not post the gate-guard check run", err)

    def test_dry_run_posts_nothing(self):
        fake = mock.Mock()
        fake.get.return_value = {"merge_base_commit": {"sha": "e" * 40}}
        fake.pages.return_value = []
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp, "event.json")
            path.write_text(json.dumps(self.EVENT), encoding="utf-8")
            env = clean_env({"GITHUB_EVENT_PATH": str(path), "GITHUB_REPOSITORY": REPO})
            with mock.patch.dict(os.environ, env, clear=True), mock.patch("sys.stdout", io.StringIO()), \
                    mock.patch.object(gate_guard, "fetch_diff", return_value=""):
                code = gate_guard.run_target(gate_guard.parse_args(["--pull-request-target", "--dry-run"]), fake)
        self.assertEqual(code, 0)
        fake.post.assert_not_called()

    def test_annotations_only_for_lines(self):
        hits = [gate_guard.Hit("retry", "a.ts", 3, "raised"), gate_guard.Hit("removed-tests", "b.py", None, "gone")]
        self.assertEqual(gate_guard.check_annotations(hits, approved=False), [
            {"path": "a.ts", "start_line": 3, "end_line": 3, "annotation_level": "failure",
             "title": "gate-guard retry", "message": "raised"}])


class FetchDiff(unittest.TestCase):
    def test_token_reaches_git_only_through_the_environment(self):
        calls = []

        def run(argv, **kwargs):
            calls.append((argv, kwargs.get("env", {})))
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        with mock.patch.dict(os.environ, {"GIT_CONFIG_COUNT": "2"}), \
                mock.patch.object(gate_guard.subprocess, "run", side_effect=run):
            gate_guard.fetch_diff("https://github.com/o/r.git", "s3cret", "a" * 40, "b" * 40)
        fetch_argv, env = calls[1]
        self.assertEqual(fetch_argv[3:5], ["fetch", "-q"])
        self.assertIn("--depth=1", fetch_argv)
        # init, fetch and diff only: nothing is checked out.
        self.assertEqual([next(a for a in ("init", "fetch", "diff", "checkout") if a in argv) for argv, _ in calls],
                         ["init", "fetch", "diff"])
        self.assertTrue(all("s3cret" not in " ".join(argv) for argv, _ in calls))
        self.assertEqual(env["GIT_CONFIG_COUNT"], "3")
        self.assertEqual(env["GIT_CONFIG_KEY_2"], "http.extraheader")
        self.assertTrue(env["GIT_CONFIG_VALUE_2"].startswith("AUTHORIZATION: basic "))


class WorkflowSafety(unittest.TestCase):
    """Static rules for the pull_request_target workflows: base-only judge, no PR code, no secrets."""

    def text(self, path):
        return path.read_text(encoding="utf-8")

    def test_reusable_workflow(self):
        text = self.text(WORKFLOWS / "gate-guard-target.yml")
        self.assertRegex(text, r"(?m)^on:\n  workflow_call:")
        self.assertNotIn("secrets.", text)
        self.assertNotIn("github.event.pull_request", text)
        for uses in re.findall(r"(?m)^\s*(?:- )?uses:\s*(\S+)", text):
            self.assertRegex(uses, r"@[0-9a-f]{40}$", f"{uses} is not pinned to a full SHA")
        checkouts = text.count("uses: actions/checkout@")
        self.assertEqual(checkouts, 1)
        self.assertEqual(text.count("persist-credentials: false"), checkouts)
        self.assertIn("repository: dflippojr/.github", text)
        self.assertIn("if: github.event_name != 'pull_request_target'", text)
        self.assertIn("grep -Eqx '[0-9a-f]{40}'", text)

    def test_callers(self):
        for path in (WORKFLOWS / "gate-guard-base.yml", ROOT / "templates" / "gate-guard-target-caller.yml"):
            with self.subTest(caller=path.name):
                text = self.text(path)
                self.assertRegex(text, r"(?m)^on:\n  pull_request_target:\n")
                self.assertNotRegex(text, r"(?m)^  (pull_request|push|workflow_run):")
                self.assertNotIn("actions/checkout", text)
                self.assertNotIn("secrets", text)
                self.assertNotRegex(text, r"head\.(sha|ref)")
                block = re.search(r"(?m)^permissions:\n((?:  .*\n)+)", text).group(1)
                self.assertEqual(block.split(), ["contents:", "read", "issues:", "read", "pull-requests:", "read",
                                                 "checks:", "write"])

    def test_template_pins_one_sha_in_both_places(self):
        text = self.text(ROOT / "templates" / "gate-guard-target-caller.yml")
        self.assertIn("uses: dflippojr/.github/.github/workflows/gate-guard-target.yml@<full-sha>", text)
        self.assertIn("guard-ref: <full-sha>", text)

    def test_reusable_workflow_edit_is_a_gate_change(self):
        diff = ("diff --git a/.github/workflows/gate-guard-target.yml b/.github/workflows/gate-guard-target.yml\n"
                "--- a/.github/workflows/gate-guard-target.yml\n+++ b/.github/workflows/gate-guard-target.yml\n"
                "@@ -1 +1 @@\n-name: Gate guard (default branch)\n+name: Gate guard\n")
        [hit] = gate_guard.analyze(gate_guard.parse_diff(diff))
        self.assertEqual((hit.rule, hit.detail), ("gate-config", "workflow modified"))


if __name__ == "__main__":
    unittest.main()
