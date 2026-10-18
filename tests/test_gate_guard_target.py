"""The pull_request_target gate guard: owner labels dated against the head push, verdicts posted on the head."""
import io
import json
import os
import pathlib
import re
import subprocess
import tempfile
import unittest
import urllib.parse
from unittest import mock

from test_gate_guard import FIXTURES, _trees, clean_env, gate_guard

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCENARIOS = pathlib.Path(__file__).resolve().parent / "fixtures" / "gate_guard_target"
WORKFLOWS = ROOT / ".github" / "workflows"
OWNER, REPO, NUMBER, HEAD_REF = "dflippojr", "dflippojr/demo", 7, "feature"
HEAD_GROUP = "group: gate-guard-target-${{ github.event.pull_request.head.sha }}"


def reject(status, body):
    return gate_guard.urllib.error.HTTPError("https://api.test", status, json.dumps(body), {}, None)


class FakeGitHub:
    """Answers the API calls of run_target and keeps the check runs it creates and updates.

    prs maps each pull request with head `head` to its base, head branch,
    labels, label events and push time, and to closed=True for one the open
    listing leaves out; merge_bases maps each base to its merge
    base with the head. Like the Checks API, it rejects an output over 65,535
    UTF-8 bytes, and any update that `refuse` returns true for.
    """

    def __init__(self, head, merge_bases, prs, refuse=lambda _: False):
        self.head, self.merge_bases, self.prs, self.refuse = head, merge_bases, prs, refuse
        self.posted = []

    @classmethod
    def single(cls, merge_base, head, scenario, base="main"):
        return cls(head, {base: merge_base}, {NUMBER: dict(scenario, base=base, head_ref=HEAD_REF)})

    def get(self, path):
        for base, merge_base in self.merge_bases.items():
            if path == f"/repos/{REPO}/compare/{base}...{self.head}?per_page=1":
                return {"merge_base_commit": {"sha": merge_base}}
        raise AssertionError(f"unexpected API path {path}")

    def pages(self, path):
        if path == f"/repos/{REPO}/pulls?state=open":
            # An open pull request with another head is noise.
            other = {"number": 99, "head": {"sha": "f" * 40, "ref": "other", "repo": {"full_name": REPO}},
                     "base": {"ref": "main"}}
            return [other] + [{"number": n, "head": {"sha": self.head, "ref": p["head_ref"],
                                                     "repo": {"full_name": REPO}}, "base": {"ref": p["base"]}}
                              for n, p in self.prs.items() if not p.get("closed")]
        for number, s in self.prs.items():
            if path == f"/repos/{REPO}/issues/{number}/labels":
                return [{"name": n} for n in s["labels"]]
            if path == f"/repos/{REPO}/issues/{number}/events":
                return [{"id": i, "event": e["event"], "label": {"name": e["label"]},
                         "actor": {"login": e["actor"]}, "created_at": e["created_at"]}
                        for i, e in enumerate(s["events"])]
            ref = f"refs/heads/{s['head_ref']}"
            if path == f"/repos/{REPO}/activity?ref={ref}":
                # Newest first, as GitHub returns it; pushes of other commits and refs are noise.
                noise = [{"after": "f" * 40, "ref": ref, "timestamp": "2026-10-16T23:00:00Z"},
                         {"after": self.head, "ref": "refs/heads/other", "timestamp": "2026-10-16T23:00:00Z"}]
                push = [{"after": self.head, "ref": ref, "timestamp": s["pushed_at"]}]
                return noise + (push if s["pushed_at"] else [])
        raise AssertionError(f"unexpected API path {path}")

    @staticmethod
    def check_size(body):
        for field in ("summary", "text"):
            if len(body.get("output", {}).get(field, "").encode("utf-8")) > 65535:
                raise reject(422, {"message": f"output.{field} is too long (maximum is 65535 bytes)"})

    def post(self, path, body):
        assert path == f"/repos/{REPO}/check-runs", path
        self.check_size(body)
        self.posted.append(dict(body))
        return {"id": len(self.posted)}

    def patch(self, path, body):
        prefix = f"/repos/{REPO}/check-runs/"
        assert path.startswith(prefix), path
        self.check_size(body)
        if self.refuse(body):
            raise reject(422, {"message": "Invalid request"})
        self.posted[int(path[len(prefix):]) - 1].update(body)

    def current(self):
        """The commit's gate-guard result: the newest check run of that name, as rulesets read it."""
        return self.posted[-1]


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


def make_remote(tmp, *trees):
    """(server URL, commit shas): a repository at <tmp>/server/<REPO>.git with one commit per tree, in a line."""
    remote = pathlib.Path(tmp, "server", f"{REPO}.git")
    remote.mkdir(parents=True)
    git(remote, "init", "-q")
    git(remote, "config", "uploadpack.allowAnySHA1InWant", "true")
    shas = [commit_tree(remote, files, f"commit {i}") for i, files in enumerate(trees)]
    return pathlib.Path(tmp, "server").as_uri(), shas


def event_pr(number, head, base, head_ref=HEAD_REF, state="open"):
    return {"number": number, "state": state, "head": {"sha": head, "ref": head_ref, "repo": {"full_name": REPO}},
            "base": {"ref": base}}


def run_cli(tmp, server, event, fake):
    """(exit code, stdout, outputs) of the CLI on a pull_request_target event, against a fake API."""
    path = pathlib.Path(tmp, "event.json")
    path.write_text(json.dumps(event), encoding="utf-8")
    outputs = pathlib.Path(tmp, "outputs.txt")
    outputs.write_text("", encoding="utf-8")
    env = clean_env({"GITHUB_EVENT_PATH": str(path), "GITHUB_REPOSITORY": REPO, "GITHUB_REPOSITORY_OWNER": OWNER,
                     "GITHUB_SERVER_URL": server, "GITHUB_OUTPUT": str(outputs), "GITHUB_RUN_ID": "99"})
    out = io.StringIO()
    with mock.patch.dict(os.environ, env, clear=True), mock.patch("sys.stdout", out), \
            mock.patch.object(gate_guard, "GitHub", return_value=fake):
        code = gate_guard.main(["--pull-request-target", "--guard-sha", "a" * 40])
    return code, out.getvalue(), outputs.read_text(encoding="utf-8")


def run_scenario(scenario, tmp):
    """(exit code, stdout, fake API, head sha, outputs) of the CLI on a scenario."""
    diff = (FIXTURES / f"{scenario['diff']}.diff").read_text(encoding="utf-8")
    server, (base, head) = make_remote(tmp, *_trees(diff))
    fake = FakeGitHub.single(base, head, scenario)
    code, out, outputs = run_cli(tmp, server, {"pull_request": event_pr(NUMBER, head, "main")}, fake)
    return code, out, fake, head, outputs


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


class SharedHead(unittest.TestCase):
    """C1 deletes a test and C2 changes code on top of it. C2 is the head of every pull request here.

    Into main (merge base C0) the deletion shows; into the stacked branch at C1
    (merge base C1) it doesn't.
    """
    BILLING = "def charge(amount):\n    return amount\n"
    TEST = "from src.billing import charge\n\n\ndef test_charge():\n    assert charge(1) == 1\n"
    NO_LABELS = {"labels": [], "events": [], "pushed_at": "2026-10-16T12:00:00Z"}
    APPROVED = {"labels": [gate_guard.APPROVAL_LABEL], "events": [
        {"event": "labeled", "label": gate_guard.APPROVAL_LABEL, "actor": OWNER, "created_at": "2026-10-16T13:00:00Z"}]}

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.server, (c0, c1, self.head) = make_remote(
            self.tmp, {"src/billing.py": self.BILLING, "tests/test_billing.py": self.TEST},
            {"src/billing.py": self.BILLING}, {"src/billing.py": "def charge(amount):\n    return round(amount, 2)\n"})
        self.merge_bases = {"main": c0, "stacked": c1}

    def pr(self, base, head_ref=HEAD_REF, **labels):
        return dict(self.NO_LABELS, base=base, head_ref=head_ref, **labels)

    def judge(self, prs, action, number, base):
        """(exit code, check run, outputs) for an event on pull request `number` whose payload names `base`."""
        fake = FakeGitHub(self.head, self.merge_bases, prs)
        state = "closed" if prs[number].get("closed") else "open"
        event = {"action": action, "pull_request": event_pr(number, self.head, base, prs[number]["head_ref"], state)}
        code, _, outputs = run_cli(self.tmp, self.server, event, fake)
        [check] = fake.posted
        self.assertEqual(check["head_sha"], self.head)
        return code, check, outputs

    def test_stacked_pull_request_passes(self):
        code, check, _ = self.judge({NUMBER: self.pr("stacked")}, "opened", NUMBER, "stacked")
        self.assertEqual((code, check["conclusion"]), (0, "success"))

    def test_retarget_to_main_is_judged_against_main(self):
        """Retargeting fires `edited`, which the callers subscribe to; the deleted test now shows."""
        code, check, _ = self.judge({NUMBER: self.pr("main")}, "edited", NUMBER, "main")
        self.assertEqual((code, check["conclusion"]), (1, "failure"))
        self.assertIn(f"### Gate guard: #{NUMBER} into `main`", check["output"]["summary"])
        self.assertIn("removed-tests | `tests/test_billing.py`", check["output"]["summary"])

    def test_current_base_wins_over_the_payload(self):
        """A queued event's payload can predate a retarget; the guard judges the base the API reports now."""
        code, check, _ = self.judge({NUMBER: self.pr("main")}, "labeled", NUMBER, "stacked")
        self.assertEqual((code, check["conclusion"]), (1, "failure"))
        self.assertIn("removed-tests | `tests/test_billing.py`", check["output"]["summary"])

    def test_passing_pull_request_cannot_replace_a_failing_one(self):
        """A second pull request with this head, into the stacked branch, passes alone; the commit's check fails."""
        prs = {NUMBER: self.pr("main"), 8: self.pr("stacked", head_ref="stacked-copy")}
        for number, base in ((8, "stacked"), (NUMBER, "main")):
            with self.subTest(event=number):
                code, check, outputs = self.judge(prs, "opened", number, base)
                self.assertEqual((code, check["conclusion"]), (1, "failure"))
                self.assertEqual(check["output"]["title"], f"#{NUMBER} into main: 1 hit, not approved")
                summary = check["output"]["summary"]
                self.assertIn(f"2 open pull requests have head `{self.head[:12]}`", summary)
                self.assertIn(f"Failing: #{NUMBER}.", summary)
                self.assertIn("### Gate guard: #8 into `stacked`", summary)
                self.assertIn("conclusion=failure", outputs)

    def test_shared_head_passes_once_every_pull_request_does(self):
        prs = {NUMBER: self.pr("main", **self.APPROVED), 8: self.pr("stacked", head_ref="stacked-copy")}
        code, check, outputs = self.judge(prs, "opened", 8, "stacked")
        self.assertEqual((code, check["conclusion"]), (0, "success"))
        self.assertEqual(check["output"]["title"], "All 2 pull requests with this head pass")
        self.assertIn("Failing: none.", check["output"]["summary"])
        # The outputs say whether a label counts on every pull request with this head.
        self.assertIn("gate-change-approved=false", outputs)

    def test_closing_the_failing_pull_request_rejudges_the_rest(self):
        """closed re-judges the head: the pull requests still open with it decide, not the one that closed."""
        prs = {NUMBER: self.pr("main", closed=True), 8: self.pr("stacked", head_ref="stacked-copy")}
        code, check, outputs = self.judge(prs, "closed", NUMBER, "main")
        self.assertEqual((code, check["conclusion"]), (0, "success"))
        self.assertEqual(check["output"]["title"], "No removed tests, skips, retries or gate-config edits")
        self.assertNotIn(f"#{NUMBER}", check["output"]["summary"])
        self.assertIn("conclusion=success", outputs)

    def test_closed_pull_request_the_listing_still_shows_is_left_out(self):
        """The listing can lag the close; the payload says the pull request is closed."""
        prs = {NUMBER: self.pr("main"), 8: self.pr("stacked", head_ref="stacked-copy")}
        fake = FakeGitHub(self.head, self.merge_bases, prs)
        event = {"action": "closed", "pull_request": event_pr(NUMBER, self.head, "main", state="closed")}
        code, _, _ = run_cli(self.tmp, self.server, event, fake)
        [check] = fake.posted
        self.assertEqual((code, check["conclusion"]), (0, "success"))
        self.assertNotIn(f"#{NUMBER}", check["output"]["summary"])

    def test_closing_the_last_pull_request_with_the_head_posts_no_verdict(self):
        code, check, outputs = self.judge({NUMBER: self.pr("main", closed=True)}, "closed", NUMBER, "main")
        self.assertEqual((code, check["conclusion"]), (0, "neutral"))
        self.assertEqual(check["output"]["title"], "No open pull request has this head")
        self.assertNotIn(f"#{NUMBER}", check["output"]["summary"])
        self.assertNotIn("removed-tests", check["output"]["summary"])
        self.assertIn("conclusion=neutral", outputs)
        self.assertIn("gate-change-approved=false", outputs)
        self.assertIn("findings-accepted=false", outputs)


class OversizedReport(unittest.TestCase):
    """The review's scenario: C1 deletes a test and adds 300 skipped test modules with long non-ASCII names; C2
    changes code on top. Into the stacked branch at C1 the head passes. Retargeted to main, the failure report runs
    to about 50,000 characters but twice that in UTF-8 bytes, over the Checks API's 65,535-byte limit."""
    BILLING = SharedHead.BILLING
    SKIPPED = "import pytest\n\n\n@pytest.mark.skip\ndef test_later():\n    pass\n"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        skipped = {f"tests/test_{i}_{'界' * 70}.py": self.SKIPPED for i in range(300)}
        self.server, (c0, c1, self.head) = make_remote(
            self.tmp, {"src/billing.py": self.BILLING, "tests/test_billing.py": SharedHead.TEST},
            dict(skipped, **{"src/billing.py": self.BILLING}),
            dict(skipped, **{"src/billing.py": "def charge(amount):\n    return round(amount, 2)\n"}))
        self.merge_bases = {"main": c0, "stacked": c1}

    def retarget(self, refuse=lambda _: False):
        """(exit code of the retarget's job, fake API) after a pass into the stacked branch, then a retarget."""
        fake = FakeGitHub(self.head, self.merge_bases, {NUMBER: dict(SharedHead.NO_LABELS, base="stacked",
                                                                     head_ref=HEAD_REF)})
        code, _, _ = run_cli(self.tmp, self.server, {"action": "opened",
                                                     "pull_request": event_pr(NUMBER, self.head, "stacked")}, fake)
        self.assertEqual((code, fake.current()["conclusion"]), (0, "success"))
        fake.prs[NUMBER]["base"], fake.refuse = "main", refuse
        code, _, _ = run_cli(self.tmp, self.server, {"action": "edited",
                                                     "pull_request": event_pr(NUMBER, self.head, "main")}, fake)
        self.assertEqual(len(fake.posted), 2)
        return code, fake

    def test_report_is_cut_to_fit_in_bytes(self):
        code, fake = self.retarget()
        check = fake.current()
        summary = check["output"]["summary"]
        self.assertEqual((code, check["status"], check["conclusion"]), (1, "completed", "failure"))
        self.assertEqual(check["output"]["title"], "301 hits, not approved")
        # Rows sort by path, so the deleted test's row is past the cut; the title still counts it.
        self.assertIn(f"| skip-marker | `tests/test_0_{'界' * 70}.py:4` |", summary)
        self.assertTrue(summary.endswith(gate_guard.TRUNCATED))
        self.assertLessEqual(len(summary.encode("utf-8")), gate_guard.MAX_CHECK_BYTES)

    def test_rejected_report_falls_back_to_a_bare_failure(self):
        code, fake = self.retarget(refuse=lambda body: "annotations" in body["output"])
        check = fake.current()
        self.assertEqual((code, check["status"], check["conclusion"]), (2, "completed", "failure"))
        self.assertEqual(check["output"]["title"], "Could not post the report")

    def test_no_update_lands_and_the_check_stays_in_progress(self):
        """Even when GitHub takes no update at all, the earlier success is no longer the commit's result."""
        code, fake = self.retarget(refuse=lambda _: True)
        check = fake.current()
        self.assertEqual((code, check["status"]), (2, "in_progress"))
        self.assertNotIn("conclusion", check)


class FitBytes(unittest.TestCase):
    def test_cut_counts_bytes_and_keeps_whole_characters(self):
        text = gate_guard.fit_bytes("界" * 100, limit=50, suffix="…")
        self.assertLessEqual(len(text.encode("utf-8")), 50)
        self.assertEqual(text, "界" * 15 + "…")

    def test_short_text_is_unchanged(self):
        self.assertEqual(gate_guard.fit_bytes("界" * 10, limit=30), "界" * 10)


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
    @staticmethod
    def github():
        """A mock API whose check-run create answers with an id; the verdict arrives as an update."""
        fake = mock.Mock()
        fake.post.return_value = {"id": 5}
        return fake

    @staticmethod
    def run_target(fake, event):
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
        fake = self.github()
        fake.get.side_effect = gate_guard.urllib.error.URLError("offline")
        fake.pages.return_value = []
        code, _ = self.run_target(fake, self.EVENT)
        self.assertEqual(code, 2)
        body = fake.patch.call_args[0][1]
        self.assertEqual((body["conclusion"], body["output"]["title"]), ("failure", "Could not judge this pull request"))
        self.assertIn("could not read the pull request's diff", body["output"]["summary"])

    def test_unreadable_labels_post_a_failure(self):
        fake = self.github()
        fake.get.return_value = {"merge_base_commit": {"sha": "e" * 40}}
        fake.pages.side_effect = gate_guard.urllib.error.URLError("rate limited")
        with mock.patch.object(gate_guard, "fetch_diff", return_value=""):
            code, _ = self.run_target(fake, self.EVENT)
        self.assertEqual(code, 2)
        self.assertIn("could not read the pull request's labels", fake.patch.call_args[0][1]["output"]["summary"])

    def test_deleted_fork_cannot_date_the_label(self):
        fake = self.github()
        fake.get.return_value = {"merge_base_commit": {"sha": "e" * 40}}
        fake.pages.side_effect = lambda path: (
            [{"name": "findings-accepted"}] if path.endswith("/labels") else
            [{"id": 1, "event": "labeled", "label": {"name": "findings-accepted"}, "actor": {"login": "dflippojr"},
              "created_at": "2026-10-16T13:00:00Z"}])
        with mock.patch.object(gate_guard, "fetch_diff", return_value=""):
            code, _ = self.run_target(fake, self.EVENT)
        self.assertEqual(code, 1)
        self.assertIn("GitHub has no record of when", fake.patch.call_args[0][1]["output"]["summary"])

    def test_check_run_post_failure_exits_2(self):
        fake = self.github()
        fake.get.return_value = {"merge_base_commit": {"sha": "e" * 40}}
        fake.pages.return_value = []
        fake.post.side_effect = gate_guard.urllib.error.URLError("forbidden")
        with mock.patch.object(gate_guard, "fetch_diff", return_value=""):
            code, err = self.run_target(fake, self.EVENT)
        self.assertEqual(code, 2)
        self.assertIn("could not post the gate-guard check run", err)

    def test_dry_run_posts_nothing(self):
        fake = self.github()
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
        fake.patch.assert_not_called()

    def test_unlisted_open_pull_requests_fail_closed(self):
        """Without the whole list of pull requests with this head, one that fails could go unseen."""
        fake = self.github()
        fake.get.return_value = {"merge_base_commit": {"sha": "e" * 40}}

        def pages(path):
            if path == f"/repos/{REPO}/pulls?state=open":
                raise gate_guard.IncompleteList("too many pages")
            return []

        fake.pages.side_effect = pages
        with mock.patch.object(gate_guard, "fetch_diff", return_value=""):
            code, _ = self.run_target(fake, self.EVENT)
        self.assertEqual(code, 2)
        body = fake.patch.call_args[0][1]
        self.assertEqual(body["conclusion"], "failure")
        self.assertIn("could not list the open pull requests with this head (too many pages)",
                      body["output"]["summary"])

    def test_closed_event_with_an_unlisted_head_fails_closed(self):
        """With the closed pull request left out and no list, nothing would be judged; that can't pass."""
        fake = self.github()
        fake.pages.side_effect = gate_guard.urllib.error.URLError("rate limited")
        event = {"action": "closed", "pull_request": dict(self.EVENT["pull_request"], state="closed")}
        code, _ = self.run_target(fake, event)
        self.assertEqual(code, 2)
        body = fake.patch.call_args[0][1]
        self.assertEqual((body["conclusion"], body["output"]["title"]),
                         ("failure", "Could not list the open pull requests with this head"))
        self.assertIn("could not list the open pull requests with this head (<urlopen error rate limited>)",
                      body["output"]["summary"])

    def test_annotations_only_for_lines(self):
        hits = [gate_guard.Hit("retry", "a.ts", 3, "raised"), gate_guard.Hit("removed-tests", "b.py", None, "gone")]
        self.assertEqual(gate_guard.check_annotations(hits, approved=False), [
            {"path": "a.ts", "start_line": 3, "end_line": 3, "annotation_level": "failure",
             "title": "gate-guard retry", "message": "raised"}])


class CutOffLabelHistory(unittest.TestCase):
    """The owner applied the label after the push, within the first pages of label events. A worker then churned
    other labels past the page limit and removed and re-applied the owner's label there."""

    def run_with_pages(self, label, max_pages):
        head = "d" * 40
        owner = {"event": "labeled", "label": {"name": label}, "actor": {"login": OWNER},
                 "created_at": "2026-10-16T13:00:00Z"}
        churn = {"event": "labeled", "label": {"name": "ready"}, "actor": {"login": "worker[bot]"},
                 "created_at": "2026-10-16T14:00:00Z"}
        worker = [{"event": e, "label": {"name": label}, "actor": {"login": "worker[bot]"},
                   "created_at": "2026-10-16T15:00:00Z"} for e in ("unlabeled", "labeled")]
        last = gate_guard.MAX_API_PAGES + 1
        requested = []

        def request(url, _token, method="GET", _body=None):
            parts = urllib.parse.urlsplit(url)
            if method == "POST":
                return {"id": 1}, None
            if "/compare/" in parts.path:
                return {"merge_base_commit": {"sha": "e" * 40}}, None
            if parts.path.endswith("/labels"):
                return [{"name": label}], None
            if parts.path.endswith("/activity"):
                return [{"after": head, "ref": f"refs/heads/{HEAD_REF}", "timestamp": "2026-10-16T12:00:00Z"}], None
            if parts.path.endswith("/events"):
                page = int(urllib.parse.parse_qs(parts.query).get("page", ["1"])[0])
                requested.append(page)
                events = [owner] if page == 1 else worker if page == last else [churn] * 100
                link = f'<https://api.test{parts.path}?page={page + 1}>; rel="next"' if page < last else None
                return [dict(e, id=page * 1000 + i) for i, e in enumerate(events)], link
            return [], None  # the open pull requests

        github = gate_guard.GitHub("https://api.test", "tok")
        event = {"pull_request": event_pr(NUMBER, head, "main")}
        with mock.patch.object(gate_guard, "api_request", side_effect=request), \
                mock.patch.object(gate_guard, "MAX_API_PAGES", max_pages), \
                mock.patch.object(gate_guard, "fetch_diff", return_value=""):
            code, _ = TargetFailures.run_target(github, event)
        return code, github, requested

    def test_cut_off_history_fails_closed(self):
        for label in gate_guard.OWNER_LABELS:
            with self.subTest(label=label), mock.patch.object(gate_guard.GitHub, "patch") as patch:
                code, _, requested = self.run_with_pages(label, gate_guard.MAX_API_PAGES)
                self.assertEqual(code, 2)
                self.assertEqual(max(requested), gate_guard.MAX_API_PAGES)
                body = patch.call_args[0][1]
                self.assertEqual(body["conclusion"], "failure")
                self.assertIn("could not read the pull request's labels", body["output"]["summary"])
                self.assertIn(f"has more than {gate_guard.MAX_API_PAGES} pages", body["output"]["summary"])

    def test_whole_history_finds_the_worker_reapplication(self):
        """The control: read to the end, the newest event is the worker's, so the label doesn't count."""
        for label in gate_guard.OWNER_LABELS:
            with self.subTest(label=label), mock.patch.object(gate_guard.GitHub, "patch") as patch:
                code, _, _ = self.run_with_pages(label, gate_guard.MAX_API_PAGES + 1)
                # The diff is empty, so only a findings-accepted label that doesn't count fails the check.
                self.assertEqual(code, 1 if label == gate_guard.FINDINGS_LABEL else 0)
                self.assertIn(f"`{label}` was applied by @worker[bot], not the owner",
                              patch.call_args[0][1]["output"]["summary"])


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
                # The head SHA only names the concurrency group: one decision at a time per commit.
                self.assertEqual(text.count(HEAD_GROUP), 1)
                self.assertNotRegex(text.replace(HEAD_GROUP, ""), r"head\.(sha|ref)")
                block = re.search(r"(?m)^permissions:\n((?:  .*\n)+)", text).group(1)
                self.assertEqual(block.split(), ["contents:", "read", "issues:", "read", "pull-requests:", "read",
                                                 "checks:", "write"])

    def test_callers_rejudge_every_change_to_base_head_or_labels(self):
        """edited covers a retarget: the same head against a new base can expose new hits. closed re-judges
        the pull requests left open with a head when one of them closes."""
        for path in (WORKFLOWS / "gate-guard-base.yml", ROOT / "templates" / "gate-guard-target-caller.yml"):
            with self.subTest(caller=path.name):
                types = re.search(r"(?m)^    types: \[(.*)\]$", self.text(path)).group(1).split(", ")
                self.assertEqual(types, ["opened", "synchronize", "reopened", "edited", "labeled", "unlabeled", "closed"])

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
