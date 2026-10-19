"""SYNTHETIC: explicit integration proof with a separately verified gitleaks CLI.

Run: python tests/prove_secret_scan.py /path/to/gitleaks
No network, credentials, real identifiers or real keys are used.
"""

import contextlib
import hashlib
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile

from test_secret_scan import ROOT, commit, commit_object_file, git, merge_only_history, scan


def main():
    executable = str(pathlib.Path(sys.argv[1]).resolve())
    key = "ghp_" + "aB7cD9eF2gH4iJ6kL8mN0pQ3rS5tU1vW9xY2"
    with tempfile.TemporaryDirectory() as temporary:
        repo = pathlib.Path(temporary)
        git(repo, "init", "-q")
        git(repo, "config", "user.email", "synthetic@example.invalid")
        git(repo, "config", "user.name", "Synthetic")
        commit(repo, "initial.txt", "clean\n")
        merge_base, _, _ = merge_only_history(repo)
        merge_hits = scan.scan_gitleaks(repo, merge_base, "HEAD", executable)
        assert ("merge-only.txt", 1, "github-pat") in merge_hits
        assert ("side-only.txt", 1, "github-pat") in merge_hits
        print("PASS: real gitleaks catches merge-only and removed side-branch PATs")
        commit(repo, "old.txt", "password=" + key + "\n")
        base = git(repo, "rev-parse", "HEAD")
        commit(repo, "new.txt", "clean\n")
        assert not scan.scan_gitleaks(repo, base, "HEAD", executable)
        print("PASS: gitleaks excludes secrets that exist only on the base")
        commit(repo, "planted.txt", "token=" + key + " # gitleaks:allow\n")
        planted = git(repo, "rev-parse", "HEAD")
        commit(repo, ".gitleaksignore", planted + ":planted.txt:github-pat:1\n")
        commit(repo, ".gitleaks.toml", 'title="empty PR rules"\n')
        commit(repo, "planted.txt", "removed\n")
        hits = scan.scan_gitleaks(repo, base, "HEAD", executable)
        assert ("planted.txt", 1, "github-pat") in hits
        print("PASS: planted fake PAT caught after deletion, despite PR config/ignore/allow")
        commit(repo, "identifier.txt", "fake-desktop\n")
        os.environ["SECRET_SCAN_DENYLIST_SHA256"] = scan.digest("fake-desktop")
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = scan.main(["--repo-dir", str(repo), "--base", base, "--gitleaks", executable])
        assert result == 1
        assert key not in output.getvalue() and "fake-desktop" not in output.getvalue()
        assert '"rule": "github-pat"' in output.getvalue()
        assert '"rule": "personal-identifier"' in output.getvalue()
        print("PASS: real gitleaks + identifier CLI fails with redacted file/line/rule only")
        commit(repo, "keys/" + key + ".txt", "token=" + key + "\n")
        card = "4111" + "1111" * 3
        commit(repo, "tests/fixtures/" + card + ".csv", "# SYNTHETIC\n" + card + "\n")
        for mode in ("--financial", "--financial-only"):
            output = io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                result = scan.main(["--repo-dir", str(repo), "--base", base, "--gitleaks", executable, mode])
            assert result == 1
            assert key not in output.getvalue() and card not in output.getvalue()
            assert '"rule": "luhn-number"' in output.getvalue()
            assert "redacted-path:" in output.getvalue()
            if mode == "--financial":
                assert '"rule": "github-pat"' in output.getvalue()
        print("PASS: real gitleaks and both financial modes keep sensitive filenames opaque")
        unusual_paths = [b":!*.txt", b":(exclude)*", b":(unknown)file.txt", b":/file.txt", b"invalid-\xff.txt"]
        for raw_path in unusual_paths:
            for renamed in (False, True):
                content = b"fake-desktop\n"
                if renamed:
                    commit_object_file(repo, b"ordinary-source.txt", content)
                base = git(repo, "rev-parse", "HEAD")
                commit_object_file(repo, raw_path, content)
                result = subprocess.run([
                    sys.executable, str(ROOT / "scripts/secret_scan.py"),
                    "--repo-dir", str(repo), "--base", base, "--gitleaks", executable,
                ], capture_output=True, text=True)
                assert result.returncode == 1
                assert "fake-desktop" not in result.stdout + result.stderr
                rows = [json.loads(line) for line in result.stdout.splitlines()[:-1]]
                assert rows == [{
                    "file": "<redacted-path:" + hashlib.sha256(raw_path).hexdigest() + ">",
                    "line": 1, "rule": "personal-identifier",
                }]
        print("PASS: real scanner CLI catches 10 pathspec/non-UTF-8 addition and rename cases")
        raw_path = b"test_invalid-\xff.csv"
        base = git(repo, "rev-parse", "HEAD")
        commit_object_file(repo, raw_path, ("# SYNTHETIC\n" + card + "\n").encode())
        for mode in ("--financial", "--financial-only"):
            result = subprocess.run([
                sys.executable, str(ROOT / "scripts/secret_scan.py"), "--repo-dir", str(repo),
                "--base", base, "--gitleaks", executable, mode,
            ], capture_output=True, text=True)
            assert result.returncode == 1
            assert card not in result.stdout + result.stderr
            rows = [json.loads(line) for line in result.stdout.splitlines()[:-1]]
            assert {row["rule"] for row in rows} >= {"luhn-number", "account-like-number"}
            expected = "<redacted-path:" + hashlib.sha256(raw_path).hexdigest() + ">"
            assert all(row["file"] == expected for row in rows)
        print("PASS: real scanner CLI reads non-UTF-8 fixture names in both financial modes")


if __name__ == "__main__":
    main()
