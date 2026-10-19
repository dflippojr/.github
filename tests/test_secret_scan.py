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
    assert json.loads(output.splitlines()[0]) == {"file": "fixture.txt", "line": 1, "rule": "personal-identifier"}


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
