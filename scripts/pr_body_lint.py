#!/usr/bin/env python3
"""Read-only PR proof checks. No PR files are downloaded or executed."""
import argparse
import base64
import fnmatch
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

import yaml

DEFAULT_TEST_PATHS = ["tests/**", "test/**", "**/*_test.*", "**/*.test.*", "**/test_*"]
CLOSES = re.compile(r"\b(?:closes|fixes|resolves)\s+(?:([\w.-]+/[\w.-]+))?#([1-9]\d*)\b", re.I)
HIT = re.compile(r"^\|\s*(?:removed-tests|skip-marker|retry|gate-config)\s*\|", re.M)


def visible_markdown(body):
    """Ignore template comments and fenced examples when looking for proof."""
    body = re.sub(r"<!--.*?(?:-->|\Z)", "", body or "", flags=re.S)
    lines, fence = [], None
    for line in body.splitlines():
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if marker:
            token = marker[1]
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            continue
        if fence is None:
            lines.append(line)
    return "\n".join(lines)


def section(body, title, level):
    heading = re.search(r"^ {0,3}" + "#" * level + r"[ \t]+" + title + r"[ \t]*#*[ \t]*$", body, re.M | re.I)
    if not heading:
        return None
    rest = body[heading.end():]
    end = re.search(r"^ {0,3}#{1," + str(level) + r"}[ \t]+", rest, re.M)
    return rest[:end.start()] if end else rest


def cells(line):
    return [c.strip() for c in re.split(r"(?<!\\)\|", line.strip().strip("|"))]


def has_table(text):
    lines = (text or "").splitlines()
    for i in range(1, len(lines) - 1):
        header, delimiter = cells(lines[i - 1]), cells(lines[i])
        if len(header) < 2 or len(header) != len(delimiter):
            continue
        if not all(re.fullmatch(r":?-{3,}:?", c) for c in delimiter):
            continue
        for row in lines[i + 1:]:
            if "|" not in row:
                break
            values = cells(row)
            if len(values) == len(header) and any(values):
                return True
    return False


def matches(path, glob):
    # A leading **/ also matches repository-root files, as in git globs.
    return fnmatch.fnmatchcase(path, glob) or (glob.startswith("**/") and fnmatch.fnmatchcase(path, glob[3:]))


def parse_rules(text):
    data = yaml.safe_load(text) if text else {}
    if data is None:
        data = {}
    if not isinstance(data, dict) or set(data) - {"test_paths", "rules"}:
        raise ValueError("pr-proof-rules.yml: use only test_paths and rules")
    paths, rules = data.get("test_paths", DEFAULT_TEST_PATHS), data.get("rules", {})
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) and p for p in paths):
        raise ValueError("pr-proof-rules.yml: test_paths must be a non-empty list of globs")
    if not isinstance(rules, dict):
        raise ValueError("pr-proof-rules.yml: rules must map globs to lists of regexes")
    compiled = {}
    for glob, patterns in rules.items():
        if not isinstance(glob, str) or not glob or not isinstance(patterns, list) or not patterns:
            raise ValueError("pr-proof-rules.yml: each glob needs a non-empty regex list")
        if not all(isinstance(p, str) and p for p in patterns):
            raise ValueError("pr-proof-rules.yml: regexes must be non-empty strings")
        compiled[glob] = [re.compile(p, re.I | re.M) for p in patterns]
    return paths, compiled


def lint(pr, files, bug, gate_findings, rules):
    body = visible_markdown(pr.get("body"))
    errors = []
    bot = pr.get("user", {}).get("login") == "dependabot[bot]"
    if not any(a.get("login", "").lower() == "dflippojr" for a in pr.get("assignees", [])):
        errors.append("Assign dflippojr to this PR.")
    if not bot:
        if not has_table(section(body, r"Proof(?: table)?", 2)):
            errors.append("Add a ## Proof table with at least one filled row.")
        if section(body, "Not verified", 3) is None:
            errors.append("Add ### Not verified.")
        if not CLOSES.search(body):
            errors.append("Add Closes #N, Fixes #N or Resolves #N.")
    if gate_findings:
        gate = section(body, "Gate changes", 3)
        if not gate or not gate.strip() or re.fullmatch(r"[\s*_`.-]*none[\s*_`.!-]*", gate, re.I):
            errors.append("Describe the gate-guard findings under ### Gate changes.")
    paths, patterns = rules
    if bug and not any(matches(f, p) for f in files for p in paths):
        errors.append("Linked bug: touch a test path (see pr-proof-rules.yml test_paths).")
    for glob, required in patterns.items():
        if any(matches(f, glob) for f in files):
            for pattern in required:
                if not pattern.search(body):
                    errors.append(f"Files matching {glob}: add proof matching {pattern.pattern}.")
    return errors


class GitHub:
    def __init__(self, token):
        self.token = token

    def get(self, path):
        request = urllib.request.Request("https://api.github.com" + path, headers={
            "Authorization": "Bearer " + self.token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)

    def pages(self, path, key=None):
        result = []
        for page in range(1, 32):
            data = self.get(path + ("&" if "?" in path else "?") + f"per_page=100&page={page}")
            items = data[key] if key else data
            result.extend(items)
            if len(items) < 100:
                return result
        raise ValueError("API pagination exceeded 3,000 items; cannot verify the entire PR")


def load_rules(api, repo):
    # Never load the head's rules: a PR must not weaken its own proof requirements.
    default = api.get(f"/repos/{repo}")["default_branch"]
    path = f"/repos/{repo}/contents/.github/pr-proof-rules.yml?ref=" + urllib.parse.quote(default, safe="")
    try:
        data = api.get(path)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return parse_rules("")
        raise
    if data.get("type") != "file" or data.get("encoding") != "base64":
        raise ValueError("pr-proof-rules.yml must be a regular, base64-encoded file")
    return parse_rules(base64.b64decode(data["content"]).decode("utf-8"))


def gate_has_findings(api, repo, sha):
    checks = api.pages(f"/repos/{repo}/commits/{sha}/check-runs?check_name=gate-guard&filter=latest", "check_runs")
    checks = [c for c in checks if c.get("name") == "gate-guard"]
    if not checks:
        return False  # The caller may not have wired gate-guard yet.
    check = max(checks, key=lambda c: c["id"])
    if check.get("status") != "completed":
        raise ValueError("gate-guard is pending; rerun pr-body after it completes")
    output = check.get("output") or {}
    report = (output.get("summary") or "") + "\n" + (output.get("text") or "")
    if HIT.search(report):
        return True  # Findings still count on an owner-approved/passing check.
    if "No removed tests, skip markers, retries or gate-config edits." in report:
        return False
    raise ValueError("gate-guard report has no recognized verdict; inspect it and rerun pr-body")


def inspect_pr(api, repo, number):
    pr = api.get(f"/repos/{repo}/pulls/{number}")
    files = api.pages(f"/repos/{repo}/pulls/{number}/files")
    if len(files) != pr["changed_files"]:
        raise ValueError("GitHub did not return every changed file; cannot verify test paths")
    paths = [f[key] for f in files for key in ("filename", "previous_filename") if f.get(key)]
    bug = False
    for match in CLOSES.finditer(visible_markdown(pr.get("body"))):
        linked_repo = match[1] or repo
        issue = api.get(f"/repos/{linked_repo}/issues/{match[2]}")
        bug = bug or any(label["name"].lower() == "bug" for label in issue.get("labels", []))
    findings = gate_has_findings(api, repo, pr["head"]["sha"])
    errors = lint(pr, paths, bug, findings, load_rules(api, repo))
    # An edit/push during the reads must not receive a verdict from an old body/head.
    current = api.get(f"/repos/{repo}/pulls/{number}")
    if any(current.get(k) != pr.get(k) for k in ("head", "body", "assignees", "labels", "base")):
        raise ValueError("PR changed during inspection; rerun pr-body")
    return errors


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--pr", type=int)
    args = parser.parse_args(argv)
    try:
        number = args.pr
        if number is None:
            with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as event:
                number = json.load(event)["pull_request"]["number"]
        if not args.repo or not re.fullmatch(r"[\w.-]+/[\w.-]+", args.repo) or not number or number < 1:
            raise ValueError("provide --repo owner/repo and --pr N")
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            raise ValueError("GITHUB_TOKEN is required (read-only permissions)")
        errors = inspect_pr(GitHub(token), args.repo, number)
        report = "pr-body: PASS\n" if not errors else "pr-body: FAIL\n" + "\n".join("- " + e for e in errors) + "\n"
        status = int(bool(errors))
    except (OSError, ValueError, KeyError, TypeError, re.error, yaml.YAMLError) as error:
        report, status = f"pr-body: cannot verify: {error}\n", 2
    print(report)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
            summary.write("\n" + report)
    return status


if __name__ == "__main__":
    sys.exit(main())
