"""Redacted PR commit scanner. Only trusted scanner code is executed."""

import argparse
import base64
import hashlib
import ipaddress
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unicodedata


SHA = re.compile(r"[0-9a-f]{40}")
TOKEN = re.compile(r"[\w@.+:/\\-]+", re.UNICODE)
NUMBER = re.compile(r"(?<!\d)(?:\d[ -]?){8,}\d(?!\d)")


class ScanError(Exception):
    """An incomplete scan must fail closed, without exposing subprocess output."""


def normalize(token):
    token = unicodedata.normalize("NFKC", token).casefold().replace("\\", "/")
    return re.sub(r"/+", "/", token).rstrip("./")


def digest(token):
    return hashlib.sha256(normalize(token).encode("utf-8")).hexdigest()


def load_denylist(value):
    hashes = value.split()
    if not hashes or any(not re.fullmatch(r"[0-9a-f]{64}", item) for item in hashes):
        raise ScanError("denylist must contain newline-separated lowercase SHA-256 hashes")
    return set(hashes)


def candidates(text):
    # Emit whole paths, every path prefix, email addresses and their hostnames.
    # JSON-escaped Windows separators normalize to the same path components.
    for match in TOKEN.finditer(unicodedata.normalize("NFKC", text)):
        token = normalize(match.group()).strip(".")
        yield token
        parts = token.split("/")
        for end in range(1, len(parts) + 1):
            prefix = "/".join(parts[:end])
            yield prefix
            yield "/" + prefix
        for part in parts:
            yield part
            if "@" in part:
                yield part.rsplit("@", 1)[-1]
            if ":" in part and not part.endswith(":"):
                yield part.split(":", 1)[0]


def identifier_rules(text, denylist):
    rules = set()
    for token in candidates(text):
        if digest(token) in denylist:
            rules.add("personal-identifier")
        if token.endswith(".ts.net"):
            rules.add("tailnet-name")
        try:
            address = ipaddress.ip_address(token)
        except ValueError:
            continue
        # RFC1918 and IPv6 ULA; documentation addresses and localhost are allowed.
        networks = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
        if any(address in ipaddress.ip_network(net) for net in networks):
            rules.add("home-lan-address")
    return rules


def luhn(number):
    if not 13 <= len(number) <= 19:
        return False
    total = 0
    for index, char in enumerate(reversed(number)):
        value = int(char)
        if index % 2:
            value *= 2
            value -= 9 if value > 9 else 0
        total += value
    return total % 10 == 0


def aba(number):
    if len(number) != 9:
        return False
    return sum(int(char) * (3, 7, 1)[index % 3] for index, char in enumerate(number)) % 10 == 0


def financial_rules(text):
    rules = set()
    for match in NUMBER.finditer(text):
        number = re.sub(r"\D", "", match.group())
        if luhn(number):
            rules.add("luhn-number")
        if aba(number):
            rules.add("aba-routing-number")
        if len(number) >= 10:
            rules.add("account-like-number")
    return rules


def git(repo, *args, env=None):
    result = subprocess.run(
        ["git", "-C", str(repo), "-c", "core.quotePath=false", *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, check=False,
    )
    if result.returncode:
        raise ScanError("git operation failed; scan incomplete")
    return result.stdout


def resolve(repo, ref):
    value = git(repo, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}").decode().strip()
    if not SHA.fullmatch(value):
        raise ScanError("invalid commit")
    return value


def added_lines(repo, base, head):
    for commit in git(repo, "rev-list", "--reverse", f"{base}..{head}").decode().splitlines():
        parents = git(repo, "rev-list", "--parents", "-n", "1", commit).decode().split()
        parent = parents[1]
        paths = git(repo, "diff", "--name-only", "-z", "--no-renames", "--diff-filter=AMT", parent, commit)
        for raw_path in paths.split(b"\0"):
            if not raw_path:
                continue
            path = raw_path.decode("utf-8", errors="replace")
            yield path, 1, path
            patch = git(repo, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--unified=0", parent, commit, "--", path)
            line_number = None
            for line in patch.decode("utf-8", errors="replace").splitlines():
                hunk = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", line)
                if hunk:
                    line_number = int(hunk[1])
                elif line_number is not None and line.startswith("+"):
                    yield path, line_number, line[1:]
                    line_number += 1
                elif line_number is not None and line.startswith(" "):
                    line_number += 1


def fixture_path(path):
    parts = pathlib.PurePosixPath(path).parts
    return any(part in {"test", "tests", "fixtures", "__tests__"} for part in parts) or pathlib.PurePosixPath(path).name.startswith("test_")


def requires_synthetic_header(path):
    name = pathlib.PurePosixPath(path)
    return "fixtures" in name.parts or name.stem.endswith("_fixtures")


def scan_financial(repo, head):
    findings = set()
    for raw_path in git(repo, "ls-tree", "-r", "--name-only", "-z", head).split(b"\0"):
        path = raw_path.decode("utf-8", errors="replace")
        if not path or not fixture_path(path):
            continue
        content = git(repo, "show", f"{head}:{path}").decode("utf-8", errors="replace")
        lines = content.splitlines()
        if requires_synthetic_header(path) and not any(re.search(r"\bSYNTHETIC\b", line) for line in lines[:5]):
            findings.add((path, 1, "missing-synthetic-header"))
        for index, line in enumerate(lines, 1):
            findings.update((path, index, rule) for rule in financial_rules(line))
    return findings


def scan_gitleaks(repo, base, head, executable):
    # Point at the object database, where PR .gitleaksignore/config cannot apply.
    git_dir = git(repo, "rev-parse", "--absolute-git-dir").decode().strip()
    with tempfile.TemporaryDirectory() as temporary:
        folder = pathlib.Path(temporary)
        config = folder / "gitleaks.toml"
        config.write_text("[extend]\nuseDefault = true\n", encoding="utf-8")
        report = folder / "report.json"
        result = subprocess.run([
            executable, "git", git_dir, "--config", str(config),
            "--log-opts", f"--no-ext-diff --no-textconv {base}..{head}",
            "--ignore-gitleaks-allow", "--redact=100", "--no-banner",
            "--log-level=fatal", "--report-format=json", "--report-path", str(report),
            "--exit-code=42",
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if result.returncode not in (0, 42) or not report.exists():
            raise ScanError("gitleaks failed; scan incomplete")
        data = json.loads(report.read_text(encoding="utf-8")) or []
        return {(item["File"], int(item["StartLine"]), item["RuleID"]) for item in data}


def fetch_event(repo):
    event = json.loads(pathlib.Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    if os.environ.get("GITHUB_EVENT_NAME") != "pull_request":
        raise ScanError("only pull_request events are supported")
    pr = event["pull_request"]
    base, head = pr["base"]["sha"], pr["head"]["sha"]
    repository = os.environ["GITHUB_REPOSITORY"]
    if not all(SHA.fullmatch(sha) for sha in (base, head)) or not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
        raise ScanError("invalid event commit or repository")
    git(repo, "init", "--bare", "--quiet")
    token = base64.b64encode(("x-access-token:" + os.environ["GITHUB_TOKEN"]).encode()).decode()
    env = {**os.environ, "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader", "GIT_CONFIG_VALUE_0": "AUTHORIZATION: basic " + token, "GIT_TERMINAL_PROMPT": "0"}
    git(repo, "fetch", "--quiet", "--no-tags", f"https://github.com/{repository}.git", base, head, env=env)
    return base, head


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-dir", default=".")
    parser.add_argument("--base")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--gitleaks", default="gitleaks")
    parser.add_argument("--financial", action="store_true", help="scan all test/fixture contents at head")
    parser.add_argument("--financial-only", action="store_true")
    parser.add_argument("--github-event", action="store_true")
    args = parser.parse_args(argv)
    try:
        with tempfile.TemporaryDirectory() as temporary:
            repo = pathlib.Path(temporary) if args.github_event else pathlib.Path(args.repo_dir).resolve()
            base_ref, head_ref = fetch_event(repo) if args.github_event else (args.base, args.head)
            head = resolve(repo, head_ref)
            findings = set()
            if not args.financial_only:
                denylist = load_denylist(os.environ.get("SECRET_SCAN_DENYLIST_SHA256", ""))
                if not base_ref:
                    raise ScanError("base commit is required")
                base = git(repo, "merge-base", resolve(repo, base_ref), head).decode().strip()
                findings.update(scan_gitleaks(repo, base, head, args.gitleaks))
                for path, line, text in added_lines(repo, base, head):
                    findings.update((path, line, rule) for rule in identifier_rules(text, denylist))
            if args.financial or args.financial_only:
                findings.update(scan_financial(repo, head))
            for path, line, rule in sorted(findings):
                if not args.financial_only and identifier_rules(path, denylist):
                    path = "<redacted-path:" + hashlib.sha256(path.encode()).hexdigest() + ">"
                # JSON escaping prevents file names injecting workflow commands.
                print(json.dumps({"file": path, "line": line, "rule": rule}, ensure_ascii=True))
            print(f"secret-scan: {len(findings)} finding(s)")
            return 1 if findings else 0
    except (ScanError, OSError, ValueError, KeyError, TypeError):
        print("secret-scan: configuration or scanner error; scan incomplete", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
