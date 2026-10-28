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
    """Answers the API calls of run_target and keeps the verdict of each job run against it.

    prs maps each pull request with head `head` to its base, head branch,
    labels, label events, comments and push time, and to closed=True for a closed PR.
    listed overrides its visibility in the open listing to simulate API lag;
    head_sha overrides its current head. merge_bases maps each base to its merge
    base with the head. Like the API for a token without checks: write, it
    refuses every write, and keeps the path of each one tried.
    """

    def __init__(self, head, merge_bases, prs):
        self.head, self.merge_bases, self.prs = head, merge_bases, prs
        self.jobs, self.writes = [], []

    @classmethod
    def single(cls, merge_base, head, scenario, base="main"):
        return cls(head, {base: merge_base}, {NUMBER: dict(scenario, base=base, head_ref=HEAD_REF)})

    def get(self, path):
        for number in self.prs:
            if path == f"/repos/{REPO}/pulls/{number}":
                return self.pull_request(number)
        for base, merge_base in self.merge_bases.items():
            if path == f"/repos/{REPO}/compare/{base}...{self.head}?per_page=1":
                return {"merge_base_commit": {"sha": merge_base}}
        raise AssertionError(f"unexpected API path {path}")

    def pull_request(self, number):
        pr = self.prs[number]
        return event_pr(number, pr.get("head_sha", self.head), pr["base"], pr["head_ref"],
                        "closed" if pr.get("closed") else "open")

    def pages(self, path):
        if path == f"/repos/{REPO}/pulls?state=open":
            # An open pull request with another head is noise.
            other = {"number": 99, "head": {"sha": "f" * 40, "ref": "other", "repo": {"full_name": REPO}},
                     "base": {"ref": "main"}}
            return [other] + [dict(self.pull_request(n), state="open") for n, p in self.prs.items()
                              if p.get("listed", not p.get("closed"))]
        for number, s in self.prs.items():
            if path == f"/repos/{REPO}/issues/{number}/labels":
                return [{"name": n} for n in s["labels"]]
            if path == f"/repos/{REPO}/issues/{number}/events":
                return [{"id": i, "event": e["event"], "label": {"name": e["label"]},
                         "actor": {"login": e["actor"]}, "created_at": e["created_at"]}
                        for i, e in enumerate(s["events"])]
            if path == f"/repos/{REPO}/issues/{number}/comments":
                # {head} in a body stands for the head sha, which the scenario's git trees decide.
                return [{"id": i, "user": {"login": c["author"]}, "body": c["body"].format(head=self.head),
                         "created_at": c["created_at"], "updated_at": c.get("updated_at", c["created_at"])}
                        for i, c in enumerate(s.get("comments", []))]
            ref = f"refs/heads/{s['head_ref']}"
            if path == f"/repos/{REPO}/activity?ref={ref}":
                # Newest first, as GitHub returns it; pushes of other commits and refs are noise.
                noise = [{"after": "f" * 40, "ref": ref, "timestamp": "2026-10-16T23:00:00Z"},
                         {"after": self.head, "ref": "refs/heads/other", "timestamp": "2026-10-16T23:00:00Z"}]
                push = [{"after": self.head, "ref": ref, "timestamp": s["pushed_at"]}]
                return noise + (push if s["pushed_at"] else [])
        raise AssertionError(f"unexpected API path {path}")

    def post(self, path, body):
        self.writes.append(path)
        raise reject(403, {"message": "Resource not accessible by integration"})

    patch = post

    def current(self):
        """The commit's gate-guard-target / gate-guard result: the newest job's, as rulesets read it."""
        return self.jobs[-1]


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
    # No auto maintenance: git commit starts it detached, and it can write to .git/objects while the caller's
    # temporary directory is removed.
    git(root, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "maintenance.auto=false", "commit", "-qm",
        message, "--allow-empty")
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


VERDICT = "::notice title=gate-guard verdict::"


def commands(stdout):
    """The workflow commands the runner would act on: lines starting with ::, outside a stop-commands block."""
    out, resume = [], None
    for line in stdout.splitlines():
        if resume:
            resume = None if line == resume else resume
        elif line.startswith("::stop-commands::"):
            resume = f"::{line[len('::stop-commands::'):]}::"
        elif line.startswith("::"):
            out.append(line)
    return out


def _unescape(text):
    for escaped, char in (("%0A", "\n"), ("%0D", "\r"), ("%3A", ":"), ("%2C", ","), ("%25", "%")):
        text = text.replace(escaped, char)
    return text


def job_verdict(stdout, outputs, summary):
    """What the job's own check shows: its conclusion, the title of its one verdict or error annotation, and the
    job summary. A verdict notice reads hits=N; conclusion; title, and an error's title property is the title."""
    found = [c for c in commands(stdout) if c.startswith((VERDICT, "::error title="))]
    assert len(found) == 1, found
    if found[0].startswith(VERDICT):
        title = _unescape(found[0][len(VERDICT):]).split("; ", 2)[2]
    else:
        title = _unescape(re.match(r"::error title=([^:]*)::", found[0]).group(1))
    conclusion = re.search(r"(?m)^conclusion=(\w+)$", outputs).group(1)
    return {"conclusion": conclusion, "title": title, "summary": summary}


def run_job(tmp, event, github, server=None):
    """(exit code, job verdict or None, stdout, outputs, stderr) of the guard as the job runs it."""
    path = pathlib.Path(tmp, "event.json")
    path.write_text(json.dumps(event), encoding="utf-8")
    outputs, summary = pathlib.Path(tmp, "outputs.txt"), pathlib.Path(tmp, "summary.md")
    outputs.write_text("", encoding="utf-8")
    summary.write_text("", encoding="utf-8")
    env = clean_env({"GITHUB_EVENT_PATH": str(path), "GITHUB_REPOSITORY": REPO, "GITHUB_REPOSITORY_OWNER": OWNER,
                     "GITHUB_OUTPUT": str(outputs), "GITHUB_STEP_SUMMARY": str(summary), "GITHUB_ACTIONS": "true",
                     **({"GITHUB_SERVER_URL": server} if server else {})})
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict(os.environ, env, clear=True), mock.patch("sys.stdout", out), mock.patch("sys.stderr", err),             mock.patch.object(gate_guard, "GitHub", return_value=github):
        code = gate_guard.main(["--pull-request-target", "--guard-sha", "a" * 40])
    stdout, written = out.getvalue(), outputs.read_text(encoding="utf-8")
    verdict = job_verdict(stdout, written, summary.read_text(encoding="utf-8")) if "conclusion=" in written else None
    return code, verdict, stdout, written, err.getvalue()


def run_cli(tmp, server, event, fake):
    """(exit code, stdout, outputs) of the guard on a pull_request_target event; fake.current() has its verdict."""
    code, verdict, out, outputs, _ = run_job(tmp, event, fake, server)
    fake.jobs.append(verdict)
    return code, out, outputs


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
        self.assertLessEqual({"head-edits-caller", "stale-owner-label", "bot-label", "owner-label-after-head",
                              "approve-gate-no-record", "approve-gate-push-race", "approve-gate-head-record",
                              "approve-gate-record-without-command", "approve-gate-hand-label-after-workflow"},
                             names)

    def test_scenarios(self):
        for path in sorted(SCENARIOS.glob("*.json")):
            scenario = json.loads(path.read_text(encoding="utf-8"))
            with self.subTest(scenario=path.stem), tempfile.TemporaryDirectory() as tmp:
                code, out, fake, head, outputs = run_scenario(scenario, tmp)
                expect = scenario["expect"]
                self.assertEqual(code, expect["exit"], out)
                [check] = fake.jobs
                self.assertEqual(fake.writes, [])
                self.assertIn(f"Judged head `{head[:12]}`", check["summary"])
                self.assertEqual(check["conclusion"], expect["conclusion"])
                self.assertEqual(check["title"], expect["title"])
                self.assertIn(expect["reason"].format(head=head[:7]), check["summary"])
                self.assertIn(f"conclusion={expect['conclusion']}", outputs)

    def test_head_edits_caller_is_judged_by_the_base(self):
        """The head's caller would pass the job; the default branch's guard fails it."""
        scenario = json.loads((SCENARIOS / "head-edits-caller.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            code, _, fake, _, _ = run_scenario(scenario, tmp)
        self.assertEqual(code, 1)
        summary = fake.jobs[0]["summary"]
        self.assertIn("| gate-config | `.github/workflows/gate-guard-target.yml` | workflow modified |", summary)
        self.assertIn("removed-tests | `tests/test_billing.py`", summary)
        self.assertIn("none of them run", summary)


class RecordAfterTheGuard(unittest.TestCase):
    """The label starts the guard, and approve_gate.py posts its head record a moment later. Nothing reruns the
    guard on a comment, so it waits a little for the record of a fresh /approve-gate label (#46)."""
    SCENARIO = json.loads((SCENARIOS / "approve-gate-head-record.json").read_text(encoding="utf-8"))

    def run_guard(self, now, record_after=None):
        """(exit code, check, comment reads, sleeps): the record shows up after record_after reads, or never."""
        scenario = dict(self.SCENARIO, comments=list(self.SCENARIO["comments"]))
        record = scenario["comments"].pop()
        reads = []

        class Late(FakeGitHub):
            def pages(self, path):
                if path.endswith("/comments"):
                    reads.append(path)
                    late = record_after is not None and len(reads) > record_after
                    self.prs[NUMBER]["comments"] = scenario["comments"] + ([record] if late else [])
                return super().pages(path)

        diff = (FIXTURES / f"{scenario['diff']}.diff").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            server, (base, head) = make_remote(tmp, *_trees(diff))
            fake = Late(head, {"main": base}, {NUMBER: dict(scenario, base="main", head_ref=HEAD_REF)})
            with mock.patch.object(gate_guard, "_now", create=True, return_value=gate_guard.parse_time(now)), \
                    mock.patch("time.sleep") as sleep:
                code, _, _ = run_cli(tmp, server, {"pull_request": event_pr(NUMBER, head, "main")}, fake)
        return code, fake.current(), len(reads), sleep.call_count

    def test_a_record_posted_after_the_first_read_counts(self):
        code, check, reads, sleeps = self.run_guard("2026-10-16T13:00:01Z", record_after=1)
        self.assertEqual((code, check["conclusion"]), (0, "success"), check["summary"])
        self.assertIn("and the owner's head record posted at 2026-10-16T13:00:01Z names", check["summary"])
        self.assertEqual((reads, sleeps), (2, 1))

    def test_no_record_still_fails_closed_after_a_bounded_wait(self):
        code, check, reads, sleeps = self.run_guard("2026-10-16T13:00:01Z")
        self.assertEqual((code, check["conclusion"]), (1, "failure"))
        self.assertIn("no unedited head record from the owner", check["summary"])
        self.assertGreater(sleeps, 0)
        self.assertLessEqual(sleeps * gate_guard.RECORD_POLL, gate_guard.RECORD_WAIT)
        self.assertEqual(reads, sleeps + 1)

    def test_an_old_label_gets_no_wait(self):
        code, check, reads, sleeps = self.run_guard("2026-10-16T13:10:00Z", record_after=1)
        self.assertEqual((code, check["conclusion"]), (1, "failure"))
        self.assertEqual((reads, sleeps), (1, 0))


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
        [check] = fake.jobs
        self.assertIn(f"Judged head `{self.head[:12]}`", check["summary"])
        return code, check, outputs

    def test_stacked_pull_request_passes(self):
        code, check, _ = self.judge({NUMBER: self.pr("stacked")}, "opened", NUMBER, "stacked")
        self.assertEqual((code, check["conclusion"]), (0, "success"))

    def test_retarget_to_main_is_judged_against_main(self):
        """Retargeting fires `edited`, which the callers subscribe to; the deleted test now shows."""
        code, check, _ = self.judge({NUMBER: self.pr("main")}, "edited", NUMBER, "main")
        self.assertEqual((code, check["conclusion"]), (1, "failure"))
        self.assertIn(f"### Gate guard: #{NUMBER} into `main`", check["summary"])
        self.assertIn("removed-tests | `tests/test_billing.py`", check["summary"])

    def test_current_base_wins_over_the_payload(self):
        """A queued event's payload can predate a retarget; the guard judges the base the API reports now."""
        code, check, _ = self.judge({NUMBER: self.pr("main")}, "labeled", NUMBER, "stacked")
        self.assertEqual((code, check["conclusion"]), (1, "failure"))
        self.assertIn("removed-tests | `tests/test_billing.py`", check["summary"])

    def test_passing_pull_request_cannot_replace_a_failing_one(self):
        """A second pull request with this head, into the stacked branch, passes alone; the commit's check fails."""
        prs = {NUMBER: self.pr("main"), 8: self.pr("stacked", head_ref="stacked-copy")}
        for number, base in ((8, "stacked"), (NUMBER, "main")):
            with self.subTest(event=number):
                code, check, outputs = self.judge(prs, "opened", number, base)
                self.assertEqual((code, check["conclusion"]), (1, "failure"))
                self.assertEqual(check["title"], f"#{NUMBER} into main: 1 hit, not approved")
                summary = check["summary"]
                self.assertIn(f"2 open pull requests have head `{self.head[:12]}`", summary)
                self.assertIn(f"Failing: #{NUMBER}.", summary)
                self.assertIn("### Gate guard: #8 into `stacked`", summary)
                self.assertIn("conclusion=failure", outputs)

    def test_shared_head_passes_once_every_pull_request_does(self):
        prs = {NUMBER: self.pr("main", **self.APPROVED), 8: self.pr("stacked", head_ref="stacked-copy")}
        code, check, outputs = self.judge(prs, "opened", 8, "stacked")
        self.assertEqual((code, check["conclusion"]), (0, "success"))
        self.assertEqual(check["title"], "All 2 pull requests with this head pass")
        self.assertIn("Failing: none.", check["summary"])
        # The outputs say whether a label counts on every pull request with this head.
        self.assertIn("gate-change-approved=false", outputs)

    def test_closing_the_failing_pull_request_rejudges_the_rest(self):
        """closed re-judges the head: the pull requests still open with it decide, not the one that closed."""
        prs = {NUMBER: self.pr("main", closed=True), 8: self.pr("stacked", head_ref="stacked-copy")}
        code, check, outputs = self.judge(prs, "closed", NUMBER, "main")
        self.assertEqual((code, check["conclusion"]), (0, "success"))
        self.assertEqual(check["title"], "No removed tests, skips, retries or gate-config edits")
        self.assertNotIn(f"#{NUMBER}", check["summary"])
        self.assertIn("conclusion=success", outputs)

    def test_closed_pull_request_the_listing_still_shows_is_left_out(self):
        """The listing can lag the close; a fresh single-PR read confirms it is closed."""
        prs = {NUMBER: self.pr("main", closed=True, listed=True), 8: self.pr("stacked", head_ref="stacked-copy")}
        fake = FakeGitHub(self.head, self.merge_bases, prs)
        event = {"action": "closed", "pull_request": event_pr(NUMBER, self.head, "main", state="closed")}
        code, _, _ = run_cli(self.tmp, self.server, event, fake)
        [check] = fake.jobs
        self.assertEqual((code, check["conclusion"]), (0, "success"))
        self.assertNotIn(f"#{NUMBER}", check["summary"])

    def test_closing_the_last_pull_request_with_the_head_posts_no_verdict(self):
        code, check, outputs = self.judge({NUMBER: self.pr("main", closed=True)}, "closed", NUMBER, "main")
        self.assertEqual((code, check["conclusion"]), (0, "neutral"))
        self.assertEqual(check["title"], "No open pull request has this head")
        self.assertNotIn(f"#{NUMBER}", check["summary"])
        self.assertNotIn("removed-tests", check["summary"])
        self.assertIn("conclusion=neutral", outputs)
        self.assertIn("gate-change-approved=false", outputs)
        self.assertIn("findings-accepted=false", outputs)

    def test_closed_rerun_after_reopening_cannot_clear_an_unapproved_head(self):
        """Reopening fails first; rerunning the older close must not replace that failure with neutral."""
        fake = FakeGitHub(self.head, self.merge_bases, {NUMBER: self.pr("main")})
        reopened = {"action": "reopened", "pull_request": event_pr(NUMBER, self.head, "main")}
        code, _, _ = run_cli(self.tmp, self.server, reopened, fake)
        self.assertEqual((code, fake.current()["conclusion"]), (1, "failure"))
        closed = {"action": "closed", "pull_request": event_pr(NUMBER, self.head, "main", state="closed")}
        code, _, outputs = run_cli(self.tmp, self.server, closed, fake)
        self.assertEqual((code, fake.current()["conclusion"]), (1, "failure"))
        self.assertIn("conclusion=failure", outputs)
        self.assertIn("gate-change-approved=false", outputs)

    def test_closed_rerun_cannot_hide_a_reopened_pull_request_sharing_a_passing_head(self):
        """The approved sibling must not let the stale close clear the reopened PR's unapproved deletion."""
        prs = {NUMBER: self.pr("main"), 8: self.pr("main", head_ref="approved-copy", **self.APPROVED)}
        fake = FakeGitHub(self.head, self.merge_bases, prs)
        reopened = {"action": "reopened", "pull_request": event_pr(NUMBER, self.head, "main")}
        code, _, _ = run_cli(self.tmp, self.server, reopened, fake)
        self.assertEqual((code, fake.current()["conclusion"]), (1, "failure"))
        closed = {"action": "closed", "pull_request": event_pr(NUMBER, self.head, "main", state="closed")}
        code, _, outputs = run_cli(self.tmp, self.server, closed, fake)
        check = fake.current()
        self.assertEqual((code, check["conclusion"]), (1, "failure"))
        self.assertIn(f"Failing: #{NUMBER}.", check["summary"])
        self.assertIn("conclusion=failure", outputs)
        self.assertIn("gate-change-approved=false", outputs)

    def test_reopened_then_edited_pull_request_uses_current_base_even_when_unlisted(self):
        """A reopen followed by a retarget must not reuse the old passing stacked base."""
        fake = FakeGitHub(self.head, self.merge_bases, {NUMBER: self.pr("stacked")})
        reopened = {"action": "reopened", "pull_request": event_pr(NUMBER, self.head, "stacked")}
        code, _, _ = run_cli(self.tmp, self.server, reopened, fake)
        self.assertEqual((code, fake.current()["conclusion"]), (0, "success"))
        fake.prs[NUMBER].update(base="main", listed=False)
        for action, state in (("edited", "open"), ("closed", "closed"), ("reopened", "open")):
            with self.subTest(action=action):
                event = {"action": action, "pull_request": event_pr(NUMBER, self.head, "stacked", state=state)}
                code, _, _ = run_cli(self.tmp, self.server, event, fake)
                self.assertEqual((code, fake.current()["conclusion"]), (1, "failure"))
                self.assertIn(f"#{NUMBER} into `main`", fake.current()["summary"])

    def test_synchronize_after_reopening_uses_current_head_and_branch(self):
        """An old close judges only siblings still at H; synchronize judges the reopened PR's new head."""
        old_head = self.merge_bases["stacked"]
        prs = {NUMBER: self.pr("main", head_ref="reopened-branch", head_sha=old_head),
               8: self.pr("main", head_ref="old-copy", head_sha=old_head, **self.APPROVED)}
        fake = FakeGitHub(old_head, self.merge_bases, prs)
        reopened = {"action": "reopened", "pull_request": event_pr(NUMBER, old_head, "main")}
        code, _, _ = run_cli(self.tmp, self.server, reopened, fake)
        self.assertEqual((code, fake.current()["conclusion"]), (1, "failure"))
        fake.prs[NUMBER]["head_sha"] = self.head
        closed = {"action": "closed", "pull_request": event_pr(NUMBER, old_head, "main", state="closed")}
        code, _, _ = run_cli(self.tmp, self.server, closed, fake)
        self.assertEqual((code, fake.current()["conclusion"]), (0, "success"))
        self.assertIn(f"Judged head `{old_head[:12]}`", fake.current()["summary"])
        self.assertNotIn(f"#{NUMBER}", fake.current()["summary"])
        fake.head = self.head
        synchronized = {"action": "synchronize", "pull_request": event_pr(NUMBER, self.head, "main")}
        code, _, outputs = run_cli(self.tmp, self.server, synchronized, fake)
        self.assertEqual((code, fake.current()["conclusion"]), (1, "failure"))
        self.assertIn(f"Judged head `{self.head[:12]}`", fake.current()["summary"])
        self.assertIn("conclusion=failure", outputs)

    def test_stale_open_payload_cannot_restore_a_currently_closed_pull_request(self):
        for action in ("reopened", "edited", "synchronize"):
            with self.subTest(action=action):
                fake = FakeGitHub(self.head, self.merge_bases, {NUMBER: self.pr("main", closed=True)})
                event = {"action": action, "pull_request": event_pr(NUMBER, self.head, "main")}
                code, _, _ = run_cli(self.tmp, self.server, event, fake)
                self.assertEqual((code, fake.current()["conclusion"]), (0, "neutral"))

    def test_closed_sibling_in_stale_listing_is_reconciled_too(self):
        prs = {NUMBER: self.pr("main", closed=True, listed=True),
               8: self.pr("stacked", head_ref="stacked-copy")}
        code, check, _ = self.judge(prs, "edited", 8, "stacked")
        self.assertEqual((code, check["conclusion"]), (0, "success"))
        self.assertNotIn(f"#{NUMBER}", check["summary"])

    def test_unresolved_current_state_fails_closed_even_with_an_approved_sibling(self):
        for number in (NUMBER, 8):
            with self.subTest(unreadable=number):
                prs = {NUMBER: self.pr("main"), 8: self.pr("main", head_ref="approved-copy", **self.APPROVED)}
                fake = FakeGitHub(self.head, self.merge_bases, prs)
                get = fake.get

                def read(path):
                    if path == f"/repos/{REPO}/pulls/{number}":
                        raise reject(403, {"message": "rate limited"})
                    return get(path)

                event = {"action": "closed", "pull_request": event_pr(NUMBER, self.head, "main", state="closed")}
                with mock.patch.object(fake, "get", side_effect=read):
                    code, _, outputs = run_cli(self.tmp, self.server, event, fake)
                self.assertEqual((code, fake.current()["conclusion"]), (2, "failure"))
                self.assertIn(f"could not read pull request #{number}'s current state",
                              fake.current()["summary"])
                self.assertIn("gate-change-approved=false", outputs)
                self.assertIn("findings-accepted=false", outputs)


class OversizedReport(unittest.TestCase):
    """The review's scenario: C1 deletes a test and adds 300 skipped test modules with long non-ASCII names; C2
    changes code on top. Into the stacked branch at C1 the head passes. Retargeted to main, the failure report runs
    to about 50,000 characters but twice that in UTF-8 bytes, over the 65,535 bytes a Checks API post could carry."""
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

    def retarget(self):
        """(exit code of the retarget's job, fake API) after a pass into the stacked branch, then a retarget."""
        fake = FakeGitHub(self.head, self.merge_bases, {NUMBER: dict(SharedHead.NO_LABELS, base="stacked",
                                                                     head_ref=HEAD_REF)})
        code, _, _ = run_cli(self.tmp, self.server, {"action": "opened",
                                                     "pull_request": event_pr(NUMBER, self.head, "stacked")}, fake)
        self.assertEqual((code, fake.current()["conclusion"]), (0, "success"))
        fake.prs[NUMBER]["base"] = "main"
        code, _, _ = run_cli(self.tmp, self.server, {"action": "edited",
                                                     "pull_request": event_pr(NUMBER, self.head, "main")}, fake)
        self.assertEqual(len(fake.jobs), 2)
        return code, fake

    def test_whole_report_reaches_the_job_summary(self):
        code, fake = self.retarget()
        check = fake.current()
        summary = check["summary"]
        self.assertEqual((code, check["conclusion"]), (1, "failure"))
        self.assertEqual(check["title"], "301 hits, not approved")
        self.assertIn("| removed-tests | `tests/test_billing.py` |", summary)
        self.assertGreater(len(summary.encode("utf-8")), 65535)
        self.assertFalse(summary.endswith(gate_guard.TRUNCATED))

    def test_report_is_cut_to_fit_the_job_summary_in_bytes(self):
        with mock.patch.object(gate_guard, "MAX_SUMMARY_BYTES", 65000):
            code, fake = self.retarget()
        check = fake.current()
        summary = check["summary"]
        self.assertEqual((code, check["conclusion"]), (1, "failure"))
        # Rows sort by path, so the deleted test's row is past the cut; the verdict still counts it.
        self.assertEqual(check["title"], "301 hits, not approved")
        self.assertIn(f"| skip-marker | `tests/test_0_{'界' * 70}.py:4` |", summary)
        self.assertTrue(summary.endswith(gate_guard.TRUNCATED))
        self.assertLessEqual(len(summary.encode("utf-8")), 65000)


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


class ApproveGateLabel(unittest.TestCase):
    """approve_gate.py (#44) applies gate-change-approved with the owner's token, then posts the head record. Its
    label counts only with an owner head record for the exact head, posted at or after the label (#46)."""
    HEAD, OTHER = "c" * 40, "a" * 40
    PUSHED = gate_guard.parse_time("2026-10-16T12:00:00Z")
    LABELED = "2026-10-16T13:00:00Z"

    @staticmethod
    def comment(body, at, author="dflippojr", edited_at=None, i=0):
        return {"id": i, "user": {"login": author}, "body": body, "created_at": at, "updated_at": edited_at or at}

    def command(self, sha=HEAD, at="2026-10-16T12:59:50Z", **kw):
        return self.comment(f"/approve-gate {sha}", at, **kw)

    def record(self, sha=HEAD, at="2026-10-16T13:00:01Z", label=gate_guard.APPROVAL_LABEL, **kw):
        return self.comment(f"Owner approved `{label}` at head {sha} by [comment](https://github.com/x). A later push "
                            f"invalidates it.\n\n<!-- owner-label {label} head={sha} -->", at, **kw)

    def ao_record(self, sha=HEAD, at="2026-10-16T13:00:01Z", label=gate_guard.APPROVAL_LABEL):
        return self.comment(f"Owner applied `{label}` at head {sha}. A later push invalidates it.\n\n"
                            f"<!-- owner-label {label} head={sha} -->", at)

    def verdict(self, comments, label=gate_guard.APPROVAL_LABEL, labeled=LABELED, pushed=PUSHED):
        events = [OwnerLabel.event("labeled", "dflippojr", labeled, label=label)]
        return gate_guard.owner_label(label, {label}, events, "dflippojr", self.HEAD, pushed, comments)

    def test_workflow_label_without_a_record_does_not_count(self):
        counts, why = self.verdict([self.command()])
        self.assertFalse(counts)
        self.assertIn("applied through `/approve-gate`, but no unedited head record from the owner", why)

    def test_workflow_label_with_a_record_for_another_sha_does_not_count(self):
        counts, why = self.verdict([self.command(), self.record(self.OTHER)])
        self.assertFalse(counts)
        self.assertIn(f"names {self.OTHER[:7]}, not the head {self.HEAD[:7]}", why)

    def test_workflow_label_with_a_record_for_the_exact_head_counts(self):
        counts, why = self.verdict([self.command(), self.record()])
        self.assertTrue(counts, why)
        self.assertIn(f"and the owner's head record posted at 2026-10-16T13:00:01Z names {self.HEAD[:7]}", why)

    def test_push_race_refuses_the_pushed_head(self):
        """The owner approves A; B is pushed before the label lands; the run dies before its record."""
        command = self.command(self.OTHER, at="2026-10-16T11:59:58Z")
        counts, why = self.verdict([command], labeled="2026-10-16T12:00:03Z")
        self.assertFalse(counts)
        self.assertIn("no unedited head record from the owner", why)
        # A run that got as far as its record names A, which is not the head either.
        counts, _ = self.verdict([command, self.record(self.OTHER, at="2026-10-16T12:00:04Z")],
                                 labeled="2026-10-16T12:00:03Z")
        self.assertFalse(counts)

    def test_the_newest_record_decides(self):
        later = "2026-10-16T13:05:00Z"
        self.assertFalse(self.verdict([self.command(), self.record(), self.record(self.OTHER, later)])[0])
        self.assertTrue(self.verdict([self.command(), self.record(self.OTHER), self.record(at=later)])[0])

    def test_a_record_from_before_the_label_does_not_count(self):
        """A record older than the label covers an earlier approval, not this label."""
        counts, why = self.verdict([self.command(), self.record(at="2026-10-16T12:59:59Z")])
        self.assertFalse(counts)
        self.assertIn("no unedited head record from the owner", why)

    def test_an_edited_record_does_not_count(self):
        counts, _ = self.verdict([self.command(), self.record(edited_at="2026-10-16T14:00:00Z")])
        self.assertFalse(counts)

    def test_someone_elses_record_does_not_count(self):
        for author in ("worker[bot]", "dflippojr-workers[bot]", "dflippojr2"):
            with self.subTest(author=author):
                self.assertFalse(self.verdict([self.command(), self.record(author=author)])[0])

    def test_a_record_for_the_other_owner_label_does_not_count(self):
        self.assertFalse(self.verdict([self.command(), self.record(label=gate_guard.FINDINGS_LABEL)])[0])

    def test_an_unparseable_record_time_does_not_count(self):
        self.assertFalse(self.verdict([self.command(), self.record(at="later")])[0])

    def test_an_edited_command_still_marks_the_label(self):
        """The workflow ignores an edited command, but the guard can't tell whether the edit came before the run."""
        self.assertFalse(self.verdict([self.command(edited_at="2026-10-16T14:00:00Z")])[0])
        self.assertFalse(self.verdict([self.comment("/approve-gate please", "2026-10-16T12:59:50Z")])[0])
        self.assertFalse(self.verdict([self.comment("  /approve-gate\n" + self.HEAD, "2026-10-16T12:59:50Z")])[0])

    def test_a_workflow_record_marks_the_label_without_its_command(self):
        """A deleted command leaves the workflow's record, posted after its label; it marks the label, so the label
        still needs an unedited record for the head."""
        counts, why = self.verdict([self.record(self.OTHER)])
        self.assertFalse(counts)
        self.assertIn(f"applied through `/approve-gate`, but the owner's head record names {self.OTHER[:7]}", why)
        counts, why = self.verdict([self.record(edited_at="2026-10-16T14:00:00Z")])
        self.assertFalse(counts)
        self.assertIn("applied through `/approve-gate`, but no unedited head record from the owner", why)
        self.assertTrue(self.verdict([self.record()])[0])

    def test_push_race_without_its_command_refuses_the_pushed_head(self):
        """The owner approves A; B is pushed; the label lands, then the record for A; the run dies before its re-read
        and someone deletes the command."""
        counts, why = self.verdict([self.record(self.OTHER, at="2026-10-16T12:00:04Z")], labeled="2026-10-16T12:00:03Z")
        self.assertFalse(counts)
        self.assertIn(f"names {self.OTHER[:7]}, not the head {self.HEAD[:7]}", why)

    def history(self, *labeled):
        """Owner label events: labeled at each time, removed a second before the next one."""
        events = []
        for i, at in enumerate(labeled):
            if i:
                removed = gate_guard.parse_time(at) - gate_guard.datetime.timedelta(seconds=1)
                events.append(OwnerLabel.event("unlabeled", "dflippojr", gate_guard._stamp(removed), i=2 * i - 1))
            events.append(OwnerLabel.event("labeled", "dflippojr", at, i=2 * i))
        return events

    def test_a_hand_label_after_an_earlier_workflow_label_keeps_todays_rule(self):
        """The owner approved A by comment at 10:00, B was pushed at 12:00, and the owner re-applied the label by
        hand at 13:00: the command and record belong to the 10:00 label, not this one."""
        comments = [self.command(self.OTHER, at="2026-10-16T10:00:00Z"),
                    self.record(self.OTHER, at="2026-10-16T10:00:03Z")]
        events = self.history("2026-10-16T10:00:02Z", self.LABELED)
        counts, why = gate_guard.owner_label(gate_guard.APPROVAL_LABEL, {gate_guard.APPROVAL_LABEL}, events,
                                             "dflippojr", self.HEAD, self.PUSHED, comments)
        self.assertTrue(counts, why)
        self.assertNotIn("/approve-gate", why)

    def test_a_workflow_label_after_an_earlier_hand_label_needs_a_record(self):
        """The workflow removes a label already on before it labels; its command still marks its own label."""
        events = self.history("2026-10-16T11:00:00Z", self.LABELED)
        counts, why = gate_guard.owner_label(gate_guard.APPROVAL_LABEL, {gate_guard.APPROVAL_LABEL}, events,
                                             "dflippojr", self.HEAD, self.PUSHED, [self.command()])
        self.assertFalse(counts)
        self.assertIn("no unedited head record from the owner", why)

    def test_a_refused_command_then_a_hand_label_needs_a_record(self):
        """The limit of the shared token: a command with no label between it and a hand label could be the run that
        applied that label and died, so the hand label needs a record (`ao owner-label apply` writes one)."""
        comments = [self.command(self.OTHER, at="2026-10-16T10:00:00Z"),
                    self.record(self.OTHER, at="2026-10-16T10:00:03Z"),
                    self.command(self.OTHER, at="2026-10-16T12:30:00Z"),
                    self.comment("`gate-change-approved` was not applied: the pull request's head is now c.",
                                 "2026-10-16T12:30:02Z")]
        events = self.history("2026-10-16T10:00:02Z", self.LABELED)
        counts, why = gate_guard.owner_label(gate_guard.APPROVAL_LABEL, {gate_guard.APPROVAL_LABEL}, events,
                                             "dflippojr", self.HEAD, self.PUSHED, comments)
        self.assertFalse(counts)
        self.assertIn("no unedited head record from the owner", why)
        counts, why = gate_guard.owner_label(gate_guard.APPROVAL_LABEL, {gate_guard.APPROVAL_LABEL}, events,
                                             "dflippojr", self.HEAD, self.PUSHED, comments + [self.ao_record()])
        self.assertTrue(counts, why)

    def test_someone_elses_command_does_not_mark_the_label(self):
        counts, why = self.verdict([self.command(author="worker[bot]")])
        self.assertTrue(counts, why)

    def test_a_command_after_the_label_does_not_mark_it(self):
        """The workflow always labels after the command it acts on; a later refused command leaves the hand label."""
        self.assertTrue(self.verdict([self.command(at="2026-10-16T13:30:00Z")])[0])

    def test_hand_label_keeps_todays_rule(self):
        self.assertTrue(self.verdict([])[0])
        self.assertTrue(self.verdict([self.ao_record(self.OTHER)])[0], "an ao record does not mark the label")
        self.assertFalse(self.verdict([], pushed=gate_guard.parse_time("2026-10-16T13:30:00Z"))[0])

    def test_findings_accepted_keeps_todays_rule(self):
        """No workflow applies findings-accepted."""
        self.assertTrue(self.verdict([self.command()], label=gate_guard.FINDINGS_LABEL)[0])

    def test_a_workflow_label_is_still_dated_against_the_push(self):
        pushed = gate_guard.parse_time("2026-10-16T13:30:00Z")
        counts, why = self.verdict([self.command(), self.record()], pushed=pushed)
        self.assertFalse(counts)
        self.assertIn("re-applies it after reviewing the new commits", why)


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
        """A mock API that answers reads; the guard makes no writes."""
        fake = mock.Mock()
        fake.get.return_value = {"merge_base_commit": {"sha": "e" * 40}}
        fake.get.side_effect = lambda path: (
            dict(TargetFailures.EVENT["pull_request"], state="open")
            if path == f"/repos/{REPO}/pulls/{NUMBER}" else fake.get.return_value)
        return fake

    @staticmethod
    def run_target(fake, event):
        """(exit code, job verdict or None, stderr) of the guard against fake."""
        with tempfile.TemporaryDirectory() as tmp:
            code, verdict, _, _, err = run_job(tmp, event, fake)
        return code, verdict, err

    EVENT = {"pull_request": {"number": NUMBER, "head": {"sha": "d" * 40, "ref": HEAD_REF, "repo": None},
                              "base": {"ref": "main"}}}

    def test_needs_a_pull_request_event(self):
        code, verdict, err = self.run_target(mock.Mock(), {})
        self.assertEqual((code, verdict), (2, None))
        self.assertIn("needs a pull_request_target event", err)

    def test_unreadable_diff_posts_a_failure(self):
        fake = self.github()
        get = fake.get.side_effect

        def read(path):
            if "/compare/" in path:
                raise gate_guard.urllib.error.URLError("offline")
            return get(path)

        fake.get.side_effect = read
        fake.pages.return_value = []
        code, body, _ = self.run_target(fake, self.EVENT)
        self.assertEqual(code, 2)
        self.assertEqual((body["conclusion"], body["title"]), ("failure", "Could not judge this pull request"))
        self.assertIn("could not read the pull request's diff", body["summary"])

    def test_unreadable_labels_post_a_failure(self):
        fake = self.github()
        fake.get.return_value = {"merge_base_commit": {"sha": "e" * 40}}
        fake.pages.side_effect = gate_guard.urllib.error.URLError("rate limited")
        with mock.patch.object(gate_guard, "fetch_diff", return_value=("", None)):
            code, body, _ = self.run_target(fake, self.EVENT)
        self.assertEqual(code, 2)
        self.assertIn("could not read the pull request's labels", body["summary"])

    def test_cut_off_comments_fail_closed(self):
        """A head record or an /approve-gate command past the page limit could change the verdict."""
        fake = self.github()

        def pages(path):
            if path.endswith("/comments"):
                raise gate_guard.IncompleteList(f"{path} has more than {gate_guard.MAX_API_PAGES} pages")
            return [{"name": gate_guard.APPROVAL_LABEL}] if path.endswith("/labels") else []

        fake.pages.side_effect = pages
        with mock.patch.object(gate_guard, "fetch_diff", return_value=("", None)):
            code, body, _ = self.run_target(fake, self.EVENT)
        self.assertEqual(code, 2)
        self.assertIn("could not read the pull request's labels", body["summary"])
        self.assertIn("/comments has more than", body["summary"])

    def test_deleted_fork_cannot_date_the_label(self):
        fake = self.github()
        fake.get.return_value = {"merge_base_commit": {"sha": "e" * 40}}
        fake.pages.side_effect = lambda path: (
            [{"name": "findings-accepted"}] if path.endswith("/labels") else
            [{"id": 1, "event": "labeled", "label": {"name": "findings-accepted"}, "actor": {"login": "dflippojr"},
              "created_at": "2026-10-16T13:00:00Z"}])
        with mock.patch.object(gate_guard, "fetch_diff", return_value=("", None)):
            code, body, _ = self.run_target(fake, self.EVENT)
        self.assertEqual(code, 1)
        self.assertIn("GitHub has no record of when", body["summary"])

    def test_nothing_is_posted(self):
        """The job's own result is the verdict; the guard writes nothing through the API."""
        fake = self.github()
        fake.pages.return_value = []
        with mock.patch.object(gate_guard, "fetch_diff", return_value=("", None)):
            code, body, _ = self.run_target(fake, self.EVENT)
        self.assertEqual((code, body["conclusion"]), (0, "success"))
        self.assertEqual({c[0] for c in fake.method_calls}, {"get", "pages"})

    def test_unlisted_open_pull_requests_fail_closed(self):
        """Without the whole list of pull requests with this head, one that fails could go unseen."""
        fake = self.github()
        fake.get.return_value = {"merge_base_commit": {"sha": "e" * 40}}

        def pages(path):
            if path == f"/repos/{REPO}/pulls?state=open":
                raise gate_guard.IncompleteList("too many pages")
            return []

        fake.pages.side_effect = pages
        with mock.patch.object(gate_guard, "fetch_diff", return_value=("", None)):
            code, body, _ = self.run_target(fake, self.EVENT)
        self.assertEqual(code, 2)
        self.assertEqual(body["conclusion"], "failure")
        self.assertIn("could not list the open pull requests with this head (too many pages)",
                      body["summary"])

    def test_closed_event_with_an_unlisted_head_fails_closed(self):
        """With the closed pull request left out and no list, nothing would be judged; that can't pass."""
        fake = self.github()
        fake.pages.side_effect = gate_guard.urllib.error.URLError("rate limited")
        event = {"action": "closed", "pull_request": dict(self.EVENT["pull_request"], state="closed")}
        fake.get.side_effect = None
        fake.get.return_value = event["pull_request"]
        code, body, _ = self.run_target(fake, event)
        self.assertEqual(code, 2)
        self.assertEqual((body["conclusion"], body["title"]),
                         ("failure", "Could not list the open pull requests with this head"))
        self.assertIn("could not list the open pull requests with this head (<urlopen error rate limited>)",
                      body["summary"])

    def test_malformed_current_pull_request_fails_closed(self):
        valid = dict(self.EVENT["pull_request"], state="open")
        responses = [None, [], {}, dict(valid, number=8), dict(valid, state="unknown"),
                     dict(valid, head=None), dict(valid, head={"sha": ""}), dict(valid, base={})]
        for response in responses:
            with self.subTest(response=response):
                fake = self.github()
                fake.pages.return_value = []
                fake.get.side_effect = None
                fake.get.return_value = response
                code, body, _ = self.run_target(fake, self.EVENT)
                self.assertEqual(code, 2)
                self.assertEqual(body["conclusion"], "failure")
                self.assertIn(f"could not read pull request #{NUMBER}'s current state", body["summary"])

    def test_verdict_comes_first_then_one_annotation_per_hit(self):
        hits = [gate_guard.Hit("retry", "a.ts", 3, "raised"), gate_guard.Hit("removed-tests", "b,c.py", None, "gone")]
        judgment = gate_guard.Judgment(NUMBER, "main", hits, None, set(), {}, False, False)
        self.assertEqual(gate_guard.verdict_commands([judgment, judgment], "failure", "2 hits, not approved", None), [
            f"{VERDICT}hits=4; failure; 2 hits, not approved",
            "::error file=a.ts,line=3,title=gate-guard retry::raised",
            "::error file=b%2Cc.py,title=gate-guard removed-tests::gone"])

    def test_an_error_gives_no_verdict(self):
        self.assertEqual(gate_guard.verdict_commands([], "failure", "Could not list: x", "offline\n::x"),
                         ["::error title=Could not list%3A x::offline%0A::x"])


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
            if parts.path == f"/repos/{REPO}/pulls/{NUMBER}":
                return event_pr(NUMBER, head, "main"), None
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
                mock.patch.object(gate_guard, "fetch_diff", return_value=("", None)):
            code, verdict, _ = TargetFailures.run_target(github, event)
        return code, verdict, requested

    def test_cut_off_history_fails_closed(self):
        for label in gate_guard.OWNER_LABELS:
            with self.subTest(label=label):
                code, body, requested = self.run_with_pages(label, gate_guard.MAX_API_PAGES)
                self.assertEqual(code, 2)
                self.assertEqual(max(requested), gate_guard.MAX_API_PAGES)
                self.assertEqual(body["conclusion"], "failure")
                self.assertIn("could not read the pull request's labels", body["summary"])
                self.assertIn(f"has more than {gate_guard.MAX_API_PAGES} pages", body["summary"])

    def test_whole_history_finds_the_worker_reapplication(self):
        """The control: read to the end, the newest event is the worker's, so the label doesn't count."""
        for label in gate_guard.OWNER_LABELS:
            with self.subTest(label=label):
                code, body, _ = self.run_with_pages(label, gate_guard.MAX_API_PAGES + 1)
                # The diff is empty, so only a findings-accepted label that doesn't count fails the check.
                self.assertEqual(code, 1 if label == gate_guard.FINDINGS_LABEL else 0)
                self.assertIn(f"`{label}` was applied by @worker[bot], not the owner", body["summary"])


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
        # init, fetch, diff and reads of the two trees only: nothing is checked out.
        self.assertEqual([next(a for a in ("init", "fetch", "diff", "ls-tree", "checkout") if a in argv)
                          for argv, _ in calls], ["init", "fetch", "diff", "ls-tree", "ls-tree"])
        self.assertTrue(all("s3cret" not in " ".join(argv) for argv, _ in calls))
        self.assertEqual(env["GIT_CONFIG_COUNT"], "3")
        self.assertEqual(env["GIT_CONFIG_KEY_2"], "http.extraheader")
        self.assertTrue(env["GIT_CONFIG_VALUE_2"].startswith("AUTHORIZATION: basic "))


class JobResult(unittest.TestCase):
    """The verdict is the judging job's own result, not a check run posted through the Checks API.

    GitHub files a posted check run under a check suite it picks: the commit's oldest github-actions suite, which can
    belong to another workflow (a Release run on wallpaper-rotator 4f90ff8) or be hidden once a later run of this
    workflow supersedes it (personal-website#240). The required check then waits forever. A job's own check lives in
    its own run's suite, so the newest run's verdict is the one a ruleset reads.
    """

    def judge(self, scenario, tmp):
        diff = (FIXTURES / f"{scenario['diff']}.diff").read_text(encoding="utf-8")
        server, (base, head) = make_remote(tmp, *_trees(diff))
        fake = FakeGitHub(head, {"main": base}, {NUMBER: dict(scenario, base="main", head_ref=HEAD_REF)})
        path = pathlib.Path(tmp, "event.json")
        path.write_text(json.dumps({"pull_request": event_pr(NUMBER, head, "main")}), encoding="utf-8")
        env = clean_env({"GITHUB_EVENT_PATH": str(path), "GITHUB_REPOSITORY": REPO, "GITHUB_REPOSITORY_OWNER": OWNER,
                         "GITHUB_SERVER_URL": server, "GITHUB_ACTIONS": "true"})
        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), mock.patch("sys.stdout", out), \
                mock.patch("sys.stderr", io.StringIO()), mock.patch.object(gate_guard, "GitHub", return_value=fake):
            code = gate_guard.main(["--pull-request-target", "--guard-sha", "a" * 40])
        return code, out.getvalue(), fake

    def test_every_scenario_is_decided_by_the_exit_status_without_a_checks_api_write(self):
        for path in sorted(SCENARIOS.glob("*.json")):
            scenario = json.loads(path.read_text(encoding="utf-8"))
            with self.subTest(scenario=path.stem), tempfile.TemporaryDirectory() as tmp:
                code, out, fake = self.judge(scenario, tmp)
                self.assertEqual(code, scenario["expect"]["exit"], out)
                self.assertEqual(fake.writes, [])

    def test_one_verdict_annotation_names_the_hits(self):
        for path in sorted(SCENARIOS.glob("*.json")):
            scenario = json.loads(path.read_text(encoding="utf-8"))
            expect = scenario["expect"]
            with self.subTest(scenario=path.stem), tempfile.TemporaryDirectory() as tmp:
                _, out, _ = self.judge(scenario, tmp)
                verdicts = [c for c in commands(out) if c.startswith(VERDICT)]
                diff = (FIXTURES / f"{scenario['diff']}.diff").read_text(encoding="utf-8")
                hits = len(gate_guard.analyze(gate_guard.parse_diff(diff)))
                self.assertEqual(verdicts, [f"{VERDICT}hits={hits}; {expect['conclusion']}; {expect['title']}"])

    def test_report_text_cannot_issue_workflow_commands(self):
        """An error message is printed in the report; a line of it that looks like a command stays inert."""
        forged = f"offline\n{VERDICT}hits=0; success; forged\n::add-mask::x"
        scenario = json.loads((SCENARIOS / "clean.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            original = FakeGitHub.get

            def get(fake, path):
                if "/compare/" in path:
                    raise gate_guard.urllib.error.URLError(forged)
                return original(fake, path)

            with mock.patch.object(FakeGitHub, "get", get):
                code, out, _ = self.judge(scenario, tmp)
        self.assertEqual(code, 2)
        self.assertIn(f"{VERDICT}hits=0; success; forged", out)
        self.assertEqual([c for c in commands(out) if c.startswith((VERDICT, "::add-mask::"))], [])


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
                self.assertEqual(block.split(), ["contents:", "read", "issues:", "read", "pull-requests:", "read"])

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

    def test_the_job_is_the_required_check_and_nothing_can_post_one(self):
        """The ruleset requires `gate-guard-target / gate-guard`: the caller's job id, then the reusable job's name.
        No token here can write checks, so neither this guard nor a step it runs can post a check run."""
        text = self.text(WORKFLOWS / "gate-guard-target.yml")
        self.assertNotIn("checks:", text)
        self.assertRegex(text, r"(?m)^  judge:\n    name: gate-guard\n")
        for path in (WORKFLOWS / "gate-guard-base.yml", ROOT / "templates" / "gate-guard-target-caller.yml"):
            with self.subTest(caller=path.name):
                self.assertRegex(self.text(path), r"(?m)^jobs:\n  gate-guard-target:\n    uses: ")
        self.assertEqual(gate_guard.REQUIRED_CHECK, "gate-guard-target / gate-guard")

    def test_reusable_workflow_edit_is_a_gate_change(self):
        diff = ("diff --git a/.github/workflows/gate-guard-target.yml b/.github/workflows/gate-guard-target.yml\n"
                "--- a/.github/workflows/gate-guard-target.yml\n+++ b/.github/workflows/gate-guard-target.yml\n"
                "@@ -1 +1 @@\n-name: Gate guard (default branch)\n+name: Gate guard\n")
        [hit] = gate_guard.analyze(gate_guard.parse_diff(diff))
        self.assertEqual((hit.rule, hit.detail), ("gate-config", "workflow modified"))


if __name__ == "__main__":
    unittest.main()
