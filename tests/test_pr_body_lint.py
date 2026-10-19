"""Proof rules, API reads and the reusable workflow's trust boundary."""
import base64
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import urllib.error
from unittest import mock

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("pr_body_lint", ROOT / "scripts/pr_body_lint.py")
lint = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lint)
FIXTURES = json.loads((ROOT / "tests/fixtures/pr_body_lint/bodies.json").read_text())
TABLES = json.loads((ROOT / "tests/fixtures/pr_body_lint/tables.json").read_text())
GOOD = FIXTURES[0]["body"]
SHA = "a" * 40


@pytest.fixture(autouse=True)
def sleeps(monkeypatch):
    """Record waits instead of sleeping; the gate poll must never really block a test."""
    waited = []
    monkeypatch.setattr("time.sleep", waited.append)
    return waited


def pr(body=GOOD, **fields):
    return dict(body=body, user={"login": "worker"}, assignees=[{"login": "dflippojr"}],
                labels=[], head={"sha": SHA}, base={"ref": "feature-base"}, changed_files=1, **fields)


@pytest.mark.parametrize("case", FIXTURES, ids=lambda c: c["name"])
def test_body_fixtures(case):
    pull = pr(case["body"])
    pull["user"]["login"] = case.get("author", "worker")
    pull["assignees"] = case.get("assignees", pull["assignees"])
    pull["labels"] = [{"name": n} for n in case.get("labels", [])]
    errors = lint.lint(pull, case.get("files", ["src/main.py"]), case.get("bug", False),
                       case.get("gate", False), lint.parse_rules(""))
    assert len(errors) == len(case["errors"]), errors
    assert all(any(want in e for e in errors) for want in case["errors"])


@pytest.mark.parametrize("fence", ["```", "~~~", "````"])
def test_fenced_proof_does_not_count(fence):
    assert len(lint.lint(pr(fence + "\n" + GOOD + "\n" + fence), [], False, False, lint.parse_rules(""))) == 3


def test_table_must_be_inside_proof_not_another_section():
    assert not lint.has_table(lint.section("## Proof\nNo evidence\n## Other\n" + GOOD, "Proof", 2))


def test_valid_table_without_outer_pipes_and_with_escaped_pipe():
    assert lint.has_table("A | B\n:--- | ---:\nproof \\| detail | passed")


@pytest.mark.parametrize("case", TABLES, ids=lambda c: c["name"])
def test_gfm_proof_table_variants(case):
    assert lint.has_table(case["table"]) == case["valid"]
    body = "Closes #20\n\n## Proof\n\n" + case["table"] + "\n\n### Not verified\nNone."
    errors = lint.lint(pr(body), [], False, False, lint.parse_rules(""))
    assert bool(errors) == (not case["valid"])


@pytest.mark.parametrize("path", ["tests/a.py", "test/nested/a.js", "foo_test.go", "a.test.ts", "test_api.py",
                                 "src/nested/foo_test.go", "src/a.test.ts", "src/test_api.py"])
def test_default_test_globs(path):
    assert not lint.lint(pr(), [path], True, False, lint.parse_rules(""))


def test_custom_globs_and_proof_patterns():
    rules = lint.parse_rules("test_paths: ['spec/**']\nrules:\n  'money/**': ['Invariant: .+']\n")
    errors = lint.lint(pr(), ["money/calc.py", "tests/test_calc.py"], True, False, rules)
    assert len(errors) == 2
    assert not lint.lint(pr(GOOD + "\nInvariant: balance conserved"), ["money/calc.py", "spec/calc.py"], True, False, rules)
    assert not lint.lint(pr(), ["README.md"], False, False, rules)


def test_dependabot_still_needs_path_specific_proof():
    pull = pr("")
    pull["user"]["login"] = "dependabot[bot]"
    assert len(lint.lint(pull, ["commands/x.md"], False, False,
                         lint.parse_rules("rules: {'commands/**': ['Eval summary: .+']}"))) == 1


@pytest.mark.parametrize("text", ["[]", "other: true", "test_paths: []", "test_paths: x", "test_paths: [3]",
                                "rules: []", "rules: {'a': 'x'}", "rules: {'a': []}",
                                "rules: {'a': [3]}", "rules: {'a': ['[']}"])
def test_invalid_rules_fail_closed(text):
    with pytest.raises((ValueError, lint.re.error)):
        lint.parse_rules(text)


def test_yaml_does_not_construct_python_objects():
    with pytest.raises(yaml.YAMLError):
        lint.parse_rules("!!python/object/apply:os.system ['echo unsafe']")


def test_readme_examples_enforce_the_four_repository_requirements():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    examples = lint.re.findall(r"```yaml\n(.*?)\n```", readme, lint.re.S)
    cases = [("money/calc.py", "Invariant: balance conserved"),
             ("src/validator.py", "Spec: https://hl7.org/fhir/validation.html"),
             ("test/goldens/example.json", "Changed goldens: example.json"),
             ("commands/build.md", "Eval summary: all cases passed")]
    examples = [text for text in examples if "\nrules:" in "\n" + text]
    assert len(examples) == len(cases)
    for text, (path, evidence) in zip(examples, cases):
        rules = lint.parse_rules(text)
        assert len(lint.lint(pr(), [path], False, False, rules)) == 1
        assert not lint.lint(pr(GOOD + "\n" + evidence), [path], False, False, rules)


def check(summary, **fields):
    return dict(id=1, name="gate-guard", status="completed", conclusion="success", output={"summary": summary}, **fields)


@pytest.mark.parametrize("conclusion", ["success", "failure"])
def test_gate_findings_count_even_when_approved(conclusion):
    api = mock.Mock()
    item = check("| Rule | Location | Detail |\n| --- | --- | --- |\n| gate-config | x | workflow added |")
    item["conclusion"] = conclusion
    api.pages.return_value = [item]
    assert lint.gate_has_findings(api, "o/r", SHA)
    assert SHA in api.pages.call_args.args[0]


def test_latest_gate_check_is_used_not_stale_findings():
    api = mock.Mock()
    old = check("| retry | file | raised |")
    latest = check("No removed tests, skip markers, retries or gate-config edits.")
    latest["id"] = 2
    api.pages.return_value = [latest, old]
    assert not lint.gate_has_findings(api, "o/r", SHA)
    api.pages.return_value = []
    with pytest.raises(ValueError, match="gate-guard.*missing"):
        lint.gate_has_findings(api, "o/r", SHA)


@pytest.mark.parametrize("item", [check("unknown report"), dict(check(""), status="in_progress")])
def test_pending_or_unrecognized_gate_does_not_pass(item):
    api = mock.Mock()
    api.pages.return_value = [item]
    with pytest.raises(ValueError):
        lint.gate_has_findings(api, "o/r", SHA)


@pytest.mark.parametrize("status", ["queued", "in_progress"])
def test_latest_pending_gate_cannot_reuse_an_older_clean_verdict(status):
    api = mock.Mock()
    api.pages.return_value = [
        check("No removed tests, skip markers, retries or gate-config edits."),
        dict(check(""), id=2, status=status),
    ]
    with pytest.raises(ValueError, match="pending"):
        lint.gate_has_findings(api, "o/r", SHA)


class FakeAPI:
    def __init__(self, *, issue_bug=False, config=None, files=None):
        self.pull = pr()
        self.issue_bug, self.config = issue_bug, config
        self.files = files if files is not None else [{"filename": "src/main.py"}]
        self.requests = []

    def get(self, path):
        self.requests.append(path)
        if path == "/repos/o/r/pulls/7":
            return copy.deepcopy(self.pull)
        if path == "/repos/o/r":
            return {"default_branch": "main"}
        if path == "/repos/o/r/contents/.github/pr-proof-rules.yml?ref=main":
            if self.config is None:
                raise urllib.error.HTTPError(path, 404, "Not Found", {}, None)
            return {"type": "file", "encoding": "base64", "content": base64.b64encode(self.config.encode()).decode()}
        if path in ("/repos/o/r/issues/20", "/repos/other/repo/issues/9"):
            return {"labels": [{"name": "bug"}] if self.issue_bug else []}
        raise AssertionError(path)

    def pages(self, path, key=None):
        self.requests.append(path)
        if path == "/repos/o/r/pulls/7/files":
            return self.files
        assert path.startswith(f"/repos/o/r/commits/{SHA}/check-runs?"), path
        return [check("No removed tests, skip markers, retries or gate-config edits.")]


CLEAN = "No removed tests, skip markers, retries or gate-config edits."


def sequence(*reports):
    """API stub whose check-runs read returns each report in turn, then repeats the last."""
    api, reads = mock.Mock(), iter(reports)
    last = []

    def pages(path, key=None):
        nonlocal last
        last = next(reads, last)
        return last

    api.pages.side_effect = pages
    return api


def test_gate_check_created_late_is_waited_for(sleeps):
    api = sequence([], [dict(check(""), status="in_progress")], [check("| gate-config | workflow.yml | added |")])
    assert lint.gate_has_findings(api, "o/r", SHA)
    assert api.pages.call_count == 3 and len(sleeps) == 2


def test_gate_check_created_late_and_clean_passes(sleeps):
    api = sequence([], [check(CLEAN)])
    assert not lint.gate_has_findings(api, "o/r", SHA)
    assert len(sleeps) == 1


def test_gate_check_pending_then_completed_passes(sleeps):
    api = sequence([dict(check(""), status="queued")], [check(CLEAN)])
    assert not lint.gate_has_findings(api, "o/r", SHA)


@pytest.mark.parametrize("reports, message", [
    ([[]], "missing"),
    ([[dict(check(""), status="in_progress")]], "pending"),
])
def test_gate_wait_is_bounded_and_then_fails_closed(sleeps, reports, message):
    api = sequence(*reports)
    with pytest.raises(ValueError, match=message):
        lint.gate_has_findings(api, "o/r", SHA)
    assert 1 <= len(sleeps) and sum(sleeps) <= 180
    assert api.pages.call_count == len(sleeps) + 1


def test_unrecognized_completed_gate_report_is_not_waited_for(sleeps):
    api = sequence([check("unknown report")], [check(CLEAN)])
    with pytest.raises(ValueError, match="recognized"):
        lint.gate_has_findings(api, "o/r", SHA)
    assert not sleeps


def test_missing_then_approved_findings_never_pass_without_gate_explanation(sleeps):
    api = FakeAPI()
    api.pull["body"] = GOOD.split("### Gate changes")[0]
    original = api.pages
    reports = iter([[], [dict(check(""), status="in_progress")], [check("| gate-config | workflow.yml | added |")]])

    def ordered_checks(path, key=None):
        return next(reports) if key == "check_runs" else original(path, key)

    api.pages = ordered_checks
    assert lint.inspect_pr(api, "o/r", 7) == [
        "Describe the gate-guard findings under ### Gate changes.",
    ]
    assert len(sleeps) == 2


def test_gate_guard_that_never_posts_fails_closed_naming_the_required_guard(sleeps):
    api = FakeAPI()
    api.pages = lambda path, key=None: [] if key == "check_runs" else FakeAPI.pages(api, path, key)
    with pytest.raises(ValueError, match="gate-guard-target"):
        lint.inspect_pr(api, "o/r", 7)


def test_inspection_reads_linked_issue_labels_and_default_branch_config():
    api = FakeAPI(issue_bug=True, config="test_paths: ['spec/**']", files=[{"filename": "spec/x.py"}])
    assert lint.inspect_pr(api, "o/r", 7) == []
    assert "/repos/o/r/issues/20" in api.requests
    assert not any("ref=feature-base" in path or "ref=" + SHA in path for path in api.requests)
    api.files = [{"filename": "src/x.py"}]
    assert "Linked bug" in lint.inspect_pr(api, "o/r", 7)[0]


def test_cross_repo_linked_bug_and_renamed_test_path():
    api = FakeAPI(issue_bug=True, files=[{"filename": "archived/x.py", "previous_filename": "tests/x.py"}])
    api.pull["body"] = GOOD.replace("Closes #20", "Fixes other/repo#9")
    assert not lint.inspect_pr(api, "o/r", 7)
    assert "/repos/other/repo/issues/9" in api.requests


def test_pr_bug_label_does_not_replace_linked_issue_label():
    api = FakeAPI()
    api.pull["labels"] = [{"name": "bug"}]
    assert not lint.inspect_pr(api, "o/r", 7)


def test_missing_files_and_mid_inspection_edits_fail_closed():
    api = FakeAPI(files=[])
    with pytest.raises(ValueError, match="every changed file"):
        lint.inspect_pr(api, "o/r", 7)
    api = FakeAPI()
    original = api.get

    def edited(path):
        result = original(path)
        if path == "/repos/o/r/pulls/7" and api.requests.count(path) == 2:
            result["body"] = "edited"
        return result

    api.get = edited
    with pytest.raises(ValueError, match="changed during inspection"):
        lint.inspect_pr(api, "o/r", 7)


def test_unrelated_repository_activity_during_inspection_is_not_a_change():
    api = FakeAPI()
    original = api.get

    def busy(path):
        result = original(path)
        if path == "/repos/o/r/pulls/7":
            n = api.requests.count(path)
            result["head"].update(repo={"pushed_at": f"t{n}", "open_issues_count": n})
            result["base"].update(sha=f"{n:040d}", repo={"pushed_at": f"t{n}", "updated_at": f"t{n}"})
        return result

    api.get = busy
    assert lint.inspect_pr(api, "o/r", 7) == []


@pytest.mark.parametrize("field, value", [
    ("head", {"sha": "b" * 40}),
    ("base", {"ref": "other-base"}),
    ("assignees", []),
    ("labels", [{"name": "gate-change-approved"}]),
])
def test_changes_the_verdict_depends_on_still_fail_closed(field, value):
    api = FakeAPI()
    original = api.get

    def changed(path):
        result = original(path)
        if path == "/repos/o/r/pulls/7" and api.requests.count(path) == 2:
            result[field] = value
        return result

    api.get = changed
    with pytest.raises(ValueError, match="changed during inspection"):
        lint.inspect_pr(api, "o/r", 7)


def test_api_pagination_and_bounds():
    api = lint.GitHub("test-token")
    with mock.patch.object(api, "get", side_effect=[[{}] * 100, [{}]]) as get:
        assert len(api.pages("/files")) == 101
        assert get.call_args.args[0] == "/files?per_page=100&page=2"
    with mock.patch.object(api, "get", return_value={"check_runs": [{}] * 100}):
        with pytest.raises(ValueError, match="pagination"):
            api.pages("/checks?filter=latest", "check_runs")


def test_api_is_get_only_with_read_token_header():
    api = lint.GitHub("test-token")
    with mock.patch.object(lint.urllib.request, "urlopen") as urlopen:
        urlopen.return_value.__enter__.return_value = io.StringIO('{"ok":true}')
        assert api.get("/repos/o/r") == {"ok": True}
        request = urlopen.call_args.args[0]
        assert request.get_method() == "GET"
        assert request.full_url == "https://api.github.com/repos/o/r"
        assert request.get_header("Authorization") == "Bearer test-token"


@pytest.mark.parametrize("errors,status", [([], 0), (["Assign dflippojr."], 1)])
def test_cli_verdicts_and_summary(tmp_path, errors, status, capsys):
    summary = tmp_path / "summary.md"
    with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "test", "GITHUB_STEP_SUMMARY": str(summary)}), \
            mock.patch.object(lint, "inspect_pr", return_value=errors):
        assert lint.main(["--repo", "o/r", "--pr", "7"]) == status
    assert ("PASS" if status == 0 else "FAIL") in summary.read_text()
    assert "pr-body:" in capsys.readouterr().out


def test_cli_reads_event_and_fails_on_api_denial(tmp_path):
    event = tmp_path / "event.json"
    event.write_text('{"pull_request":{"number":7}}')
    with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "test", "GITHUB_EVENT_PATH": str(event)}), \
            mock.patch.object(lint, "inspect_pr", side_effect=urllib.error.HTTPError("url", 403, "Forbidden", {}, None)):
        assert lint.main(["--repo", "o/r"]) == 2


def test_workflow_security_contract():
    workflow = yaml.safe_load((ROOT / ".github/workflows/pr-body-lint.yml").read_text())
    caller = yaml.safe_load((ROOT / "templates/pr-body-lint-caller.yml").read_text())
    expected = {"contents": "read", "issues": "read", "pull-requests": "read", "checks": "read"}
    assert workflow["permissions"] == caller["permissions"] == expected
    assert "workflow_call" in workflow[True]
    assert set(caller[True]) == {"pull_request_target"}
    assert {"opened", "edited", "synchronize", "reopened", "labeled"} <= set(caller[True]["pull_request_target"]["types"])
    assert workflow["jobs"]["pr-body"]["name"] == "pr-body"
    steps = workflow["jobs"]["pr-body"]["steps"]
    for step in steps:
        if "uses" in step:
            assert lint.re.fullmatch(r"actions/checkout@[0-9a-f]{40}", step["uses"])
            assert step["with"]["repository"] == "dflippojr/.github"
            assert step["with"]["ref"] == "${{ inputs.lint-ref }}"
            assert step["with"]["persist-credentials"] is False
            assert step["with"]["sparse-checkout"] == "scripts/pr_body_lint.py"
        assert "${{ github.event.pull_request" not in step.get("run", "")
    assert not any("secrets." in str(step) for step in steps)


def test_pr_body_always_runs_on_a_github_hosted_runner():
    """The job only calls the API, so a private caller must not hold its single tower runner."""
    workflow = yaml.safe_load((ROOT / ".github/workflows/pr-body-lint.yml").read_text())
    caller_text = (ROOT / "templates/pr-body-lint-caller.yml").read_text()
    caller = yaml.safe_load(caller_text)
    assert workflow["jobs"]["pr-body"]["runs-on"] == "ubuntu-latest"
    assert "runs-on" not in workflow[True]["workflow_call"]["inputs"]
    assert "runs-on" not in caller["jobs"]["pr-body"].get("with", {})
    assert "tower-linux" not in caller_text and "self-hosted" not in caller_text
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = readme[readme.index("`lint-ref` is required"):]
    section = section[:section.index("Run the fixtures")]
    assert "tower-linux" not in section and "self-hosted" not in section


PROOF_LEAKS = {
    "fenced-log-then-paragraph": "```\nlog\n```\nSee logs above.",
    "tilde-fence-then-paragraph": "~~~\nlog\n~~~\nSee logs above.",
    "thematic-break": "---",
    "underscore-thematic-break": "___",
    "spaced-thematic-break": " * * * ",
    "html-details": "<details><summary>x</summary>",
    "html-div": "<div>x</div>",
}
EMPTY_PROOF = "Closes #20\n\n## Proof\n\n| Criterion | Evidence |\n| --- | --- |\n| | |\n"


@pytest.mark.parametrize("name", PROOF_LEAKS)
def test_blocks_after_an_empty_proof_table_do_not_fill_it(name):
    body = EMPTY_PROOF + PROOF_LEAKS[name] + "\n\n### Not verified\nNone."
    assert lint.lint(pr(body), [], False, False, lint.parse_rules("")) == ["Add a ## Proof table with at least one filled row."]


def test_inline_html_and_code_rows_still_count_as_data():
    body = "Closes #20\n\n## Proof\n\n| Criterion | Evidence |\n| --- | --- |\n<code>x</code> | <b>ok</b>\n\n### Not verified\nNone."
    assert not lint.lint(pr(body), [], False, False, lint.parse_rules(""))
