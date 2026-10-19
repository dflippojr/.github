"""SYNTHETIC: generated identifiers and financial examples only."""

import hashlib
import importlib.util
import json
import os
import pathlib
import subprocess
import sys

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("secret_scan", ROOT / "scripts/secret_scan.py")
scan = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scan)


@pytest.mark.parametrize("token,text", [
    ("fake-desktop", 'hostname="FAKE-DESKTOP"'),
    ("owner@example.invalid", "Email: OWNER@example.invalid."),
    ("C:/Users/synthetic-person", r'"C:\\Users\\synthetic-person\\Documents"'),
    ("/Users/synthetic-person", "/Users/synthetic-person/docs/file.txt"),
    ("fake-desktop", "https://fake-desktop:8080/docs"),
])
def test_normalized_private_identifiers(token, text):
    assert "personal-identifier" in scan.identifier_rules(text, {scan.digest(token)})
    assert "personal-identifier" not in scan.identifier_rules(text, {scan.digest("other-person")})


@pytest.mark.parametrize("text,rule", [
    ("host.fake-tailnet.ts.net", "tailnet-name"),
    ("http://192.168.23.4:8000", "home-lan-address"),
    ("10.22.33.44", "home-lan-address"),
    ("172.16.0.1", "home-lan-address"),
    ("http://[fc00::1]:8080", "home-lan-address"),
])
def test_private_networks(text, rule):
    assert rule in scan.identifier_rules(text, set())


@pytest.mark.parametrize("text", ["192.0.2.1", "198.51.100.2", "172.32.0.1", "host.ts.net.invalid", "127.0.0.1"])
def test_documentation_and_close_network_negatives(text):
    assert not scan.identifier_rules(text, set())


@pytest.mark.parametrize("value,valid", [("4111111111111111", True), ("4111111111111112", False), ("0000000000000", True), ("411111111111", False)])
def test_luhn_positives_and_near_misses(value, valid):
    assert scan.luhn(value) is valid


@pytest.mark.parametrize("value,valid", [("021000021", True), ("021000022", False), ("000000000", True), ("02100002", False)])
def test_aba_positives_and_near_misses(value, valid):
    assert scan.aba(value) is valid


def test_formatted_financial_numbers_and_long_accounts():
    assert scan.financial_rules("card: 4111-1111-1111-1111") == {"luhn-number", "account-like-number"}
    assert scan.financial_rules("routing: 021000021") == {"aba-routing-number"}
    assert scan.financial_rules("account: 123456789012345678901234") == {"account-like-number"}
    assert not scan.financial_rules("routing: 021000022; account: 12345678")
    # A failed card checksum is still a long account-like run: do not exempt it.
    assert scan.financial_rules("4111111111111112") == {"account-like-number"}


@pytest.mark.parametrize("value", ["", "not-a-hash", "A" * 64, "123"])
def test_missing_or_invalid_denylist_fails_closed(value):
    with pytest.raises(scan.ScanError):
        scan.load_denylist(value)


def test_denylist_contains_hashes_only():
    value = scan.digest("fake-desktop")
    assert value == hashlib.sha256(b"fake-desktop").hexdigest()
    assert scan.load_denylist(value + "\n" + value) == {value}


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "synthetic@example.invalid")
    git(tmp_path, "config", "user.name", "Synthetic")
    (tmp_path / "old.txt").write_text("base-only-host\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "base")
    return tmp_path


def commit(repo, path, text):
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "synthetic change")


def test_commit_range_keeps_removed_identifiers_and_ignores_base(repo):
    base = git(repo, "rev-parse", "HEAD")
    commit(repo, "new.txt", "fake-desktop\n")
    commit(repo, "new.txt", "clean\n")
    additions = list(scan.added_lines(repo, base, "HEAD"))
    assert ("new.txt", 1, "fake-desktop") in additions
    assert not any("base-only-host" in text for _, _, text in additions)


def test_financial_scan_reads_head_objects_without_following_symlinks(repo):
    commit(repo, "tests/fixtures/bad.csv", "account\n4111111111111111\n")
    commit(repo, "tests/fixtures/good.csv", "# SYNTHETIC\nrouting: 021000022\n")
    commit(repo, "tests/test_clean.py", "# Test code without fixture data\n")
    findings = scan.scan_financial(repo, "HEAD")
    assert ("tests/fixtures/bad.csv", 1, "missing-synthetic-header") in findings
    assert ("tests/fixtures/bad.csv", 2, "luhn-number") in findings
    assert not any(path.endswith("good.csv") for path, _, _ in findings)
    assert not any(path.endswith("test_clean.py") for path, _, _ in findings)


def test_gitleaks_capture_uses_trusted_config_and_redacts_report(repo, monkeypatch):
    original = scan.subprocess.run
    secret = "ghp_" + "aB7cD9eF2gH4iJ6kL8mN0pQ3rS5tU1vW9xY2"

    def fake_run(command, **kwargs):
        if command[0] != "gitleaks":
            return original(command, **kwargs)
        assert command[2].replace("\\", "/").endswith("/.git")
        assert "--redact=100" in command
        assert "--ignore-gitleaks-allow" in command
        report = pathlib.Path(command[command.index("--report-path") + 1])
        report.write_text(json.dumps([{"File": "fixture.txt", "StartLine": 4, "RuleID": "github-pat", "Secret": secret}]))
        return subprocess.CompletedProcess(command, 42, secret.encode(), secret.encode())

    monkeypatch.setattr(scan.subprocess, "run", fake_run)
    assert scan.scan_gitleaks(repo, "HEAD", "HEAD", "gitleaks") == {("fixture.txt", 4, "github-pat")}


def test_cli_reports_only_file_line_rule(repo, monkeypatch, capsys):
    monkeypatch.setenv("SECRET_SCAN_DENYLIST_SHA256", scan.digest("fake-desktop"))
    monkeypatch.setattr(scan, "scan_gitleaks", lambda *args: set())
    base = git(repo, "rev-parse", "HEAD")
    commit(repo, "fixture.txt", "password=fake-desktop\n")
    assert scan.main(["--repo-dir", str(repo), "--base", base]) == 1
    output = capsys.readouterr().out
    assert "fake-desktop" not in output and "password" not in output
    assert json.loads(output.splitlines()[0]) == {"file": "<redacted-path:" + hashlib.sha256(b"fixture.txt").hexdigest() + ">", "line": 1, "rule": "personal-identifier"}


def test_cli_errors_do_not_print_subprocess_or_config_values(repo, monkeypatch, capsys):
    monkeypatch.setenv("SECRET_SCAN_DENYLIST_SHA256", "private-invalid-value")
    assert scan.main(["--repo-dir", str(repo), "--base", "HEAD"]) == 2
    output = capsys.readouterr()
    assert "private-invalid-value" not in output.err
    assert "scan incomplete" in output.err


def test_private_identifier_in_filename_is_not_logged(repo, monkeypatch, capsys):
    monkeypatch.setenv("SECRET_SCAN_DENYLIST_SHA256", scan.digest("fake-desktop"))
    monkeypatch.setattr(scan, "scan_gitleaks", lambda *args: set())
    base = git(repo, "rev-parse", "HEAD")
    commit(repo, "fake-desktop/config.txt", "clean\n")
    assert scan.main(["--repo-dir", str(repo), "--base", base]) == 1
    output = capsys.readouterr().out
    assert "fake-desktop" not in output
    assert "redacted-path:" in output


def test_workflow_uses_full_pins_read_only_token_and_required_secret():
    workflow = yaml.safe_load((ROOT / ".github/workflows/secret-scan.yml").read_text())
    call = workflow.get("on", workflow.get(True))["workflow_call"]
    assert call["secrets"]["SECRET_SCAN_DENYLIST_SHA256"]["required"]
    assert workflow["permissions"] == {"contents": "read"}
    steps = workflow["jobs"]["secret-scan"]["steps"]
    for step in steps:
        if "uses" in step:
            assert scan.SHA.fullmatch(step["uses"].split("@")[1])
            assert step["with"]["persist-credentials"] is False
    gitleaks = next(step for step in steps if step.get("with", {}).get("repository") == "gitleaks/gitleaks")
    assert scan.SHA.fullmatch(gitleaks["with"]["ref"])


def merge_only_history(repo):
    """Two clean parent trees, plus a side-branch secret removed before merging."""
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-qb", "synthetic-side")
    side_key = "ghp_" + "zY9xW8vU7tS6rQ5pO4nM3lK2jI1hG0fE9dC8"
    commit(repo, "side-only.txt", "token=" + side_key + "\n")
    commit(repo, "side-only.txt", "clean\n")
    git(repo, "checkout", "--detach", base)
    commit(repo, "first-parent.txt", "clean\n")
    git(repo, "merge", "--no-ff", "--no-commit", "synthetic-side")
    merge_key = "ghp_" + "aB7cD9eF2gH4iJ6kL8mN0pQ3rS5tU1vW9xY2"
    commit(repo, "merge-only.txt", "token=" + merge_key + "\n")
    return base, merge_key, side_key


def test_gitleaks_log_options_cover_merge_results_and_side_commits(repo, monkeypatch):
    base, merge_key, side_key = merge_only_history(repo)
    # Neither parent contains the merge-only value in its tree or history.
    for parent in ("HEAD^1", "HEAD^2"):
        assert merge_key not in git(repo, "log", "-p", base + ".." + parent)
        assert "merge-only.txt" not in git(repo, "ls-tree", "-r", "--name-only", parent)
    original = scan.subprocess.run
    options = []

    def fake_run(command, **kwargs):
        if command[0] != "gitleaks":
            return original(command, **kwargs)
        options.extend(command[command.index("--log-opts") + 1].split())
        report = pathlib.Path(command[command.index("--report-path") + 1])
        report.write_text("[]")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(scan.subprocess, "run", fake_run)
    scan.scan_gitleaks(repo, base, "HEAD", "gitleaks")
    patches = git(repo, "log", "-p", "-U0", *options)
    assert merge_key in patches, "merge-result patch must reach gitleaks"
    assert side_key in patches, "removed side-branch secrets must still reach gitleaks"


@pytest.mark.parametrize("kind", ["pat", "financial", "financial-only"])
def test_sensitive_filename_output_is_opaque(repo, monkeypatch, capsys, kind):
    key = "ghp_" + "aB7cD9eF2gH4iJ6kL8mN0pQ3rS5tU1vW9xY2"
    card = "4111" + "1111" * 3
    path = "keys/" + key + ".txt" if kind == "pat" else "tests/fixtures/" + card + ".csv"
    sensitive = key if kind == "pat" else card
    monkeypatch.setenv("SECRET_SCAN_DENYLIST_SHA256", scan.digest("unrelated-synthetic-host"))
    base = git(repo, "rev-parse", "HEAD")
    commit(repo, path, "# SYNTHETIC\n" + sensitive + "\n")
    monkeypatch.setattr(scan, "scan_gitleaks", lambda *args: {(path, 2, "github-pat")} if kind == "pat" else set())
    args = ["--repo-dir", str(repo), "--base", base]
    if kind != "pat":
        args.append("--" + kind)
    assert scan.main(args) == 1
    captured = capsys.readouterr()
    assert sensitive not in captured.out + captured.err, "matched value must never be logged"
    expected = "<redacted-path:" + hashlib.sha256(path.encode()).hexdigest() + ">"
    rows = [json.loads(line) for line in captured.out.splitlines()[:-1]]
    assert rows and all(row["file"] == expected for row in rows)


@pytest.mark.parametrize("text", [
    "[Contact](mailto:owner@example.invalid)",
    '<a href="MAILTO:OWNER@example.invalid">Contact</a>',
    "mailto:owner@example.invalid?subject=Hello",
    "mailto:owner@example.invalid,second@example.invalid",
])
def test_mailto_matches_denied_mailbox(text):
    denied = {scan.digest("owner@example.invalid")}
    assert "personal-identifier" in scan.identifier_rules(text, denied)
    assert not scan.identifier_rules(text, {scan.digest("other@example.invalid")})


@pytest.mark.parametrize("text", [
    "mailto:other@example.invalid",
    "mailto:prefixowner@example.invalid",
    "mailto:owner@example.invalid.example",
])
def test_mailto_mailbox_near_misses_are_allowed(text):
    assert not scan.identifier_rules(text, {scan.digest("owner@example.invalid")})


def commit_object_file(repo, raw_path, content):
    """Create untrusted names as Git objects, without platform checkout rules."""
    def object_git(*args, data):
        return subprocess.run(
            ["git", "-C", str(repo), *args], input=data,
            check=True, capture_output=True,
        ).stdout.strip()

    blob = object_git("hash-object", "-w", "--stdin", data=content)
    parts = raw_path.split(b"/")
    tree = object_git("mktree", "-z", data=b"100644 blob " + blob + b"\t" + parts[-1] + b"\0")
    for directory in reversed(parts[:-1]):
        tree = object_git("mktree", "-z", data=b"040000 tree " + tree + b"\t" + directory + b"\0")
    parent = git(repo, "rev-parse", "HEAD")
    head = object_git("commit-tree", tree.decode(), "-p", parent, data=b"synthetic change\n")
    git(repo, "update-ref", "HEAD", head.decode())


UNTRUSTED_PATHS = [
    pytest.param(b":!*.txt", id="short-exclude"),
    pytest.param(b":^*.txt", id="short-exclude-caret"),
    pytest.param(b":(exclude)*", id="long-exclude"),
    pytest.param(b":(top,exclude)*", id="top-exclude"),
    pytest.param(b":(glob)*.txt", id="glob-magic"),
    pytest.param(b":(icase)file.txt", id="icase-magic"),
    pytest.param(b":(literal)file.txt", id="literal-magic"),
    pytest.param(b":(attr:synthetic)file.txt", id="attribute-magic"),
    pytest.param(b":/file.txt", id="top-magic"),
    pytest.param(b":(unknown)file.txt", id="unknown-magic"),
    pytest.param(b":", id="empty-pathspec"),
    pytest.param(b"[ab].txt", id="bracket-glob"),
    pytest.param(b"*.txt", id="star-glob"),
    pytest.param(b"?.txt", id="question-glob"),
    pytest.param(b"back\\slash.txt", id="backslash"),
    pytest.param(b'-leading-"quote".txt', id="option-and-quotes"),
    pytest.param(b"space tab\tline\nbreak.txt", id="whitespace"),
    pytest.param("caf\u00e9.txt".encode(), id="unicode"),
    pytest.param(b"invalid-\xff.txt", id="non-utf8"),
]


@pytest.mark.parametrize("raw_path", UNTRUSTED_PATHS)
@pytest.mark.parametrize("renamed", [False, True], ids=["addition", "rename"])
def test_untrusted_filename_contents_are_scanned(repo, monkeypatch, capsys, raw_path, renamed):
    denied = "fake-desktop"
    content = (denied + "\n").encode()
    if renamed:
        commit_object_file(repo, b"ordinary-source.txt", content)
    base = git(repo, "rev-parse", "HEAD")
    commit_object_file(repo, raw_path, content)
    monkeypatch.setenv("SECRET_SCAN_DENYLIST_SHA256", scan.digest(denied))
    monkeypatch.setattr(scan, "scan_gitleaks", lambda *args: set())
    # The filename cannot satisfy the denylist: only the blob contains the value.
    assert not scan.identifier_rules(raw_path.decode("utf-8", errors="replace"), {scan.digest(denied)})
    result = scan.main(["--repo-dir", str(repo), "--base", base])
    captured = capsys.readouterr()
    assert denied not in captured.out + captured.err, "matched value must stay private"
    assert json.dumps(raw_path.decode("utf-8", errors="replace")) not in captured.out + captured.err, "filename must stay private"
    assert result == 1, "filename contents must produce a finding"
    rows = [json.loads(line) for line in captured.out.splitlines()[:-1]]
    assert rows == [{
        "file": "<redacted-path:" + hashlib.sha256(raw_path).hexdigest() + ">",
        "line": 1, "rule": "personal-identifier",
    }], "the exact filename must identify the finding opaquely"


@pytest.mark.parametrize("mode", ["--financial", "--financial-only"])
def test_non_utf8_financial_filename_is_scanned(repo, monkeypatch, capsys, mode):
    raw_path = b"test_invalid-\xff.csv"
    value = "4111" + "1111" * 3
    base = git(repo, "rev-parse", "HEAD")
    commit_object_file(repo, raw_path, ("# SYNTHETIC\n" + value + "\n").encode())
    monkeypatch.setenv("SECRET_SCAN_DENYLIST_SHA256", scan.digest("unrelated-synthetic-host"))
    monkeypatch.setattr(scan, "scan_gitleaks", lambda *args: set())
    result = scan.main(["--repo-dir", str(repo), "--base", base, mode])
    captured = capsys.readouterr()
    assert value not in captured.out + captured.err, "matched value must stay private"
    assert result == 1, "the exact fixture blob must be scanned"
    rows = [json.loads(line) for line in captured.out.splitlines()[:-1]]
    expected = "<redacted-path:" + hashlib.sha256(raw_path).hexdigest() + ">"
    assert {row["rule"] for row in rows} == {"luhn-number", "account-like-number"}
    assert all(row["file"] == expected and row["line"] == 2 for row in rows)
