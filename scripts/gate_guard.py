#!/usr/bin/env python3
"""Flag pull request changes that loosen a repository's test gate.

Reads the pull request's unified diff and reports each hit:

    removed-tests  a test file deleted, or fewer test functions in a file
    skip-marker    a new skip, focus or expected-failure marker
    retry          a new or raised retry or rerun setting
    gate-config    an edit to workflows, Sonar or coverage config, a lint
                   rule disabled, or a size or performance budget raised

Moved lines are not hits: a test renamed or moved to another file, or a skip
marker that moves with its test, cancels out. Exit status is 0 when there are
no hits or the pull request carries the gate-change-approved label, 1 when
there are unapproved hits, and 2 when the diff cannot be read. Only the
standard library is used, and only the GitHub API is contacted, to read the
label when there are hits.

    python3 gate_guard.py --repo-dir . --base HEAD^1 --head HEAD
    python3 gate_guard.py --diff-file pr.diff
"""
import argparse
import collections
import json
import os
import posixpath
import re
import subprocess
import sys
import urllib.error
import urllib.request

APPROVAL_LABEL = "gate-change-approved"
MAX_ANNOTATIONS = 50
MAX_API_PAGES = 30

Hit = collections.namedtuple("Hit", "rule path line detail")
# kind is " " (context), "-" (removed) or "+" (added); old/new are line numbers.
Line = collections.namedtuple("Line", "kind old new text")


class FileDiff:
    def __init__(self, old_path=None, new_path=None):
        self.old_path = old_path
        self.new_path = new_path
        self.status = "modified"
        self.lines = []

    @property
    def path(self):
        return self.old_path if self.status == "deleted" else self.new_path

    @property
    def paths(self):
        return [p for p in (self.old_path, self.new_path) if p]

    def changed(self, kind=None):
        return [l for l in self.lines if l.kind != " " and (kind is None or l.kind == kind)]


# ---------------------------------------------------------------- diff parsing

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _unquote(path):
    path = path.rstrip("\t")
    if len(path) >= 2 and path[0] == '"' and path[-1] == '"':
        raw = path[1:-1].encode("utf-8").decode("unicode_escape")
        path = raw.encode("latin-1").decode("utf-8", "replace")
    return path


def _strip_prefix(path, prefix):
    """The repository path from a ---/+++ line, or None for /dev/null."""
    path = _unquote(path)
    if path == "/dev/null":
        return None
    return path[len(prefix):] if path.startswith(prefix) else path


def _header_paths(rest):
    """Old and new paths from the text after 'diff --git '."""
    if rest.startswith('"'):
        end = rest.index('"', 1) + 1
        while rest[end - 2] == "\\":
            end = rest.index('"', end) + 1
        old, new = rest[:end], rest[end + 1:]
    else:
        half = (len(rest) - 1) // 2
        if len(rest) % 2 == 1 and rest[half] == " " and rest[2:half] == rest[half + 3:]:
            old, new = rest[:half], rest[half + 1:]
        else:
            old, _, new = rest.partition(" b/")
            new = "b/" + new
    return _strip_prefix(old, "a/"), _strip_prefix(new, "b/")


def parse_diff(text):
    """Parse a git-style unified diff into FileDiff objects.

    Hunk line counts decide where a hunk ends, so removed lines that look
    like headers are read correctly. Text before the first 'diff --git' line
    is ignored.
    """
    files = []
    cur = None
    old_left = new_left = old_no = new_no = 0
    for raw in text.split("\n"):
        line = raw[:-1] if raw.endswith("\r") else raw
        if cur is not None and (old_left > 0 or new_left > 0):
            kind = line[:1]
            if kind == "\\":
                continue
            if kind == "-":
                cur.lines.append(Line("-", old_no, None, line[1:]))
                old_no += 1
                old_left -= 1
                continue
            if kind == "+":
                cur.lines.append(Line("+", None, new_no, line[1:]))
                new_no += 1
                new_left -= 1
                continue
            if kind == " " or line == "":
                cur.lines.append(Line(" ", old_no, new_no, line[1:]))
                old_no += 1
                new_no += 1
                old_left -= 1
                new_left -= 1
                continue
            old_left = new_left = 0
        if line.startswith("diff --git "):
            cur = FileDiff(*_header_paths(line[len("diff --git "):]))
            files.append(cur)
            continue
        if cur is None:
            continue
        if line.startswith("new file mode"):
            cur.status = "added"
        elif line.startswith("deleted file mode"):
            cur.status = "deleted"
        elif line.startswith("rename from "):
            cur.old_path, cur.status = _unquote(line[len("rename from "):]), "renamed"
        elif line.startswith("rename to "):
            cur.new_path, cur.status = _unquote(line[len("rename to "):]), "renamed"
        elif line.startswith("--- "):
            cur.old_path = _strip_prefix(line[4:], "a/")
            if cur.old_path is None:
                cur.status = "added"
        elif line.startswith("+++ "):
            cur.new_path = _strip_prefix(line[4:], "b/")
            if cur.new_path is None:
                cur.status = "deleted"
        else:
            m = HUNK_RE.match(line)
            if m:
                old_no, new_no = int(m.group(1)), int(m.group(3))
                old_left = int(m.group(2)) if m.group(2) is not None else 1
                new_left = int(m.group(4)) if m.group(4) is not None else 1
    for fd in files:
        if fd.status == "added":
            fd.old_path = None
        elif fd.status == "deleted":
            fd.new_path = None
    return files


# ------------------------------------------------------------- classification

DOC_EXTS = {".md", ".markdown", ".rst", ".txt", ".adoc", ".diff", ".patch"}
PY_EXTS = {".py"}
JS_EXTS = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"}
JVM_EXTS = {".java", ".kt", ".groovy", ".scala"}
CODE_EXTS = PY_EXTS | JS_EXTS | JVM_EXTS | {
    ".rs", ".go", ".cs", ".rb", ".php", ".swift", ".c", ".cc", ".cpp", ".h", ".hpp",
    ".vue", ".svelte", ".sh", ".ps1"}
TEST_DIRS = {"test", "tests", "__tests__", "e2e", "spec", "specs"}
INI_NAMES = {".coveragerc", ".pylintrc", "pylintrc", ".flake8", "pytest.ini", "tox.ini",
             "setup.cfg", "mypy.ini", ".mypy.ini"}


def _name(path):
    return posixpath.basename(path).lower()


def _ext(path):
    return posixpath.splitext(_name(path))[1]


def _in_test_dir(path):
    return any(d in TEST_DIRS for d in path.lower().split("/")[:-1])


def is_doc(path):
    return _ext(path) in DOC_EXTS and not _name(path).startswith(("requirements", "constraints"))


def is_test_module(path):
    """A file whose name says it holds tests."""
    name, ext = _name(path), _ext(path)
    if ext in PY_EXTS:
        return name.startswith("test_") or name.endswith("_test.py")
    if ext in JS_EXTS:
        return bool(re.search(r"\.(test|spec|e2e)\.[^.]+$", name))
    if ext == ".go":
        return name.endswith("_test.go")
    if ext in JVM_EXTS:
        return bool(re.search(r"(tests?|it|spec)\.[^.]+$", name))
    if ext == ".rs":
        return _in_test_dir(path)
    return False


def is_test_file(path):
    """A file that holds tests or test setup: test modules, conftest.py, test directories."""
    ext = _ext(path)
    if is_test_module(path) or _name(path) == "conftest.py":
        return True
    return ext in (PY_EXTS | JS_EXTS | JVM_EXTS) and _in_test_dir(path)


def is_config(path):
    """A configuration or build file rather than application code or docs."""
    name, ext = _name(path), _ext(path)
    if is_doc(path):
        return False
    if ext not in CODE_EXTS:
        return True
    return bool(re.search(r"(\.config|rc)\.[cm]?[jt]s$", name)) or name in {"conftest.py", "noxfile.py"}


def is_ini(path):
    return _name(path) in INI_NAMES or _ext(path) in {".toml", ".cfg", ".ini"}


SECTION_RE = re.compile(r"^\s*\[\[?\s*([A-Za-z][^\]]*?)\s*\]\]?\s*(?:[#;].*)?$")


def line_sections(fd):
    """The ini/TOML section each line of fd sits in, on its own side of the diff."""
    old = new = ""
    out = []
    for l in fd.lines:
        m = SECTION_RE.match(l.text)
        if m:
            if l.kind != "+":
                old = m.group(1).lower()
            if l.kind != "-":
                new = m.group(1).lower()
        out.append(old if l.kind == "-" else new)
    return out


def _snippet(text, limit=100):
    text = text.strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _cancel_pairs(removed, added, key):
    """Drop removed/added items whose keys pair up; return what is left of each."""
    both = collections.Counter(map(key, removed)) & collections.Counter(map(key, added))

    def keep(items):
        left = collections.Counter(both)
        out = []
        for item in items:
            k = key(item)
            if left[k]:
                left[k] -= 1
            else:
                out.append(item)
        return out
    return keep(removed), keep(added)


# --------------------------------------------------------------- removed tests

PY_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(test\w*)\s*\(")
JS_DEF = re.compile(
    r"^\s*(?:test|it|xit|xtest|fit)(?:\.(?:only|skip|fixme|fail|failing|concurrent|todo|serial))*\s*\(\s*"
    r"(['\"`])(.*?)\1")
GO_DEF = re.compile(r"^\s*func\s+(Test\w*)\s*\(")
JVM_MARK = re.compile(
    r"^\s*@(?:org\.junit\.(?:jupiter\.api\.)?|kotlin\.test\.)?"
    r"(?:Test|ParameterizedTest|RepeatedTest|TestFactory|TestTemplate)\b")
JVM_NAME = re.compile(r"\b(?:void|fun)\s+(`[^`]+`|\w+)\s*\(")
RS_MARK = re.compile(r"^\s*#\[(?:[\w:]+::)?test\b")
RS_NAME = re.compile(r"\bfn\s+(\w+)")


def _line_defs(fd, regex, group):
    removed, added = [], []
    for l in fd.changed():
        m = regex.match(l.text)
        if m:
            (removed if l.kind == "-" else added).append((m.group(group), l.old or l.new))
    return removed, added


def _marker_defs(fd, marker, name_re):
    """Tests declared by an annotation on one line and named on a later line."""
    out = {"-": [], "+": []}
    # side -> whether the open marker line was itself changed
    pending = {}
    for l in fd.lines:
        for side in ("-", "+"):
            if l.kind not in (" ", side):
                continue
            changed = l.kind == side
            lineno = l.old if side == "-" else l.new
            if marker.search(l.text):
                pending[side] = changed
            elif pending.get(side) is None:
                continue
            m = name_re.search(l.text)
            if m:
                if pending[side] or changed:
                    out[side].append((m.group(1), lineno))
                pending[side] = None
            elif not marker.search(l.text):
                stripped = l.text.strip()
                if stripped and not stripped.startswith(("@", "#[", "//", "/*", "*")):
                    pending[side] = None
    return out["-"], out["+"]


def test_defs(fd):
    """(removed, added) test definitions on changed lines: lists of (name, line)."""
    ext = _ext(fd.path)
    if ext in PY_EXTS and any(is_test_file(p) for p in fd.paths):
        return _line_defs(fd, PY_DEF, 1)
    if ext in JS_EXTS and any(is_test_file(p) for p in fd.paths):
        return _line_defs(fd, JS_DEF, 2)
    if ext == ".go" and any(is_test_module(p) for p in fd.paths):
        return _line_defs(fd, GO_DEF, 1)
    if ext in JVM_EXTS:
        return _marker_defs(fd, JVM_MARK, JVM_NAME)
    if ext == ".rs":
        return _marker_defs(fd, RS_MARK, RS_NAME)
    return [], []


def _names(defs, limit=5):
    names = [n if n is not None else f"test at line {line}" for n, line in defs]
    return ", ".join(names[:limit]) + (f" and {len(names) - limit} more" if len(names) > limit else "")


def removed_test_hits(files):
    """Deleted test files and per-file decreases, after pairing renames and moves."""
    left = {}
    for fd in files:
        removed, added = test_defs(fd)
        # A test removed and re-added in the same file was edited, not removed.
        left[id(fd)] = list(_cancel_pairs(removed, added, key=lambda d: d[0]))
    # A test removed from one file and added to another was moved.
    pool = collections.defaultdict(list)
    for fd in files:
        for d in left[id(fd)][1]:
            if d[0] is not None:
                pool[d[0]].append((id(fd), d))
    for fd in files:
        removed = left[id(fd)][0]
        kept = []
        for d in removed:
            match = next((i for i, (owner, _) in enumerate(pool.get(d[0], [])) if owner != id(fd)), None)
            if match is None:
                kept.append(d)
                continue
            owner, added = pool[d[0]].pop(match)
            left[owner][1].remove(added)
        left[id(fd)][0] = kept

    hits = []
    for fd in files:
        removed, added = left[id(fd)]
        decrease = len(removed) - len(added)
        plural = "s" if decrease != 1 else ""
        if fd.status == "deleted" and (decrease > 0 or (not test_defs(fd)[0] and is_test_module(fd.path))):
            detail = "test file deleted"
            if decrease > 0:
                detail += f", {decrease} test{plural} removed: {_names(removed)}"
            hits.append(Hit("removed-tests", fd.path, None, detail))
        elif decrease > 0:
            detail = f"{decrease} fewer test{plural} in this file; removed {_names(removed)}"
            if added:
                detail += f"; added {_names(added)}"
            hits.append(Hit("removed-tests", fd.path, None, detail))
    return hits


# --------------------------------------------------------------- skip markers

def _py_tests(path):
    return _ext(path) in PY_EXTS and is_test_file(path)


def _js_tests(path):
    return _ext(path) in JS_EXTS and is_test_file(path)


SKIP_RULES = [
    ("pytest skip/xfail", _py_tests, re.compile(r"\bmark\.(?:skip|skipif|xfail)\b")),
    ("unittest skip", _py_tests, re.compile(
        r"\bunittest\.(?:skip|skipIf|skipUnless|expectedFailure)\b|^\s*@(?:skip|skipIf|skipUnless|expectedFailure)\b")),
    ("JS skip/only/fixme", _js_tests, re.compile(
        r"\.(?:skip|only|fixme)(?:If)?\s*\(|\b(?:xit|xdescribe|xtest|xcontext|fit|fdescribe)\s*\(")),
    ("JUnit @Disabled/@Ignore", lambda p: _ext(p) in JVM_EXTS, re.compile(
        r"^\s*@(?:org\.junit\.[\w.]*\.)?(?:Disabled\w*|Ignore)\b")),
    ("Rust #[ignore]", lambda p: _ext(p) == ".rs", re.compile(r"#\[ignore\b")),
    ("pytest deselect/ignore", is_config, re.compile(
        r"--deselect\b|--ignore(?:-glob)?[= ]|(?:^|\s)-[km]\s+['\"]?not\b")),
    ("test ignore pattern", is_config, re.compile(r"\btestPathIgnorePatterns\b|\btestIgnore\b")),
]
# Skips called from inside a test count only when unconditional, or when the
# condition is about CI: an environment guard such as "node isn't installed"
# is not a hit.
IMPERATIVE_SKIP_RULES = [
    ("imperative skip", _py_tests, re.compile(
        r"\bpytest\.(?:skip|xfail|importorskip)\s*\(|\.skipTest\s*\(|\braise\s+(?:unittest\.)?SkipTest\b")),
    ("Go t.Skip", lambda p: _name(p).endswith("_test.go"), re.compile(r"\.Skip(?:f|Now)?\s*\(")),
]
CONDITION_LINE = re.compile(r"^\s*(?:if|elif|else|except|unless)\b|^\s*}\s*else\b")
CI_CONDITION = re.compile(r"\bCI\b|GITHUB_ACTIONS")


def _guarded(fd):
    """ids of lines whose own text, or previous code line on their side, opens a non-CI condition."""
    guarded = set()
    previous = {"-": "", "+": ""}
    for l in fd.lines:
        sides = ("-", "+") if l.kind == " " else (l.kind,)
        for side in sides:
            if l.kind != " ":
                condition = l.text if CONDITION_LINE.match(l.text) else previous[side]
                if CONDITION_LINE.match(condition) and not CI_CONDITION.search(condition):
                    guarded.add(id(l))
            if l.text.strip() and not l.text.strip().startswith(("#", "//")):
                previous[side] = l.text
    return guarded


def _matching_lines(files, rules, imperative=False):
    """(label, path, Line) for changed lines that match a rule in scope."""
    out = []
    for fd in files:
        for label, scope, regex in rules:
            if not any(scope(p) for p in fd.paths):
                continue
            guarded = _guarded(fd) if imperative else set()
            for l in fd.changed():
                if regex.search(l.text) and id(l) not in guarded:
                    out.append((label, fd.path, l))
    return out


def _new_lines(files, rules, imperative_rules=()):
    """Added matches that are not a moved copy of a removed match anywhere in the diff."""
    found = _matching_lines(files, rules) + _matching_lines(files, imperative_rules, imperative=True)
    removed = [f for f in found if f[2].kind == "-"]
    added = [f for f in found if f[2].kind == "+"]
    return _cancel_pairs(removed, added, key=lambda f: f[2].text.strip())


def skip_hits(files):
    _, added = _new_lines(files, SKIP_RULES, IMPERATIVE_SKIP_RULES)
    return [Hit("skip-marker", path, l.new, f"{label}: {_snippet(l.text)}") for label, path, l in added]


# -------------------------------------------------------------------- retries

def _js_test_or_config(path):
    return _ext(path) in JS_EXTS and (is_test_file(path) or is_config(path))


RETRY_PRESENCE = [
    ("pytest-rerunfailures", lambda p: not is_doc(p), re.compile(r"pytest[-_]rerunfailures")),
    ("flaky", lambda p: _py_tests(p) or is_config(p), re.compile(
        r"@(?:pytest\.mark\.)?flaky\b|\bmark\.flaky\b|^\s*[\"']?flaky\s*(?:[=<>~!;\[]|[\"'],?\s*$|$)")),
]
RETRY_NUMERIC = [
    ("retries", _js_test_or_config, re.compile(r"\b(?:retries|runMode|openMode)\b[\"']?\s*[:=]\s*([^,;}\n]*)")),
    ("--retries", is_config, re.compile(r"--retries[= ]+(\d+)")),
    ("--reruns", lambda p: not is_doc(p), re.compile(r"--reruns[= ]+(\d+)|\breruns\s*=\s*(\d+)")),
    ("jest.retryTimes", lambda p: not is_doc(p), re.compile(r"\bretryTimes\s*\(\s*(\d+)")),
    ("rerunFailingTestsCount", lambda p: not is_doc(p), re.compile(
        r"rerunFailingTestsCount[\"'>]?\s*[:=>]?\s*[\"']?(\d+)")),
    ("Gradle maxRetries", lambda p: ".gradle" in _name(p), re.compile(r"\bmaxRetries\b\D*(\d+)")),
]


def _numeric_value(regex, text):
    m = regex.search(text)
    captured = " ".join(g for g in m.groups() if g) if m else ""
    numbers = [int(n) for n in re.findall(r"\d+", captured)]
    # An expression with no literal number still turns retries on.
    return max(numbers) if numbers else 1


def retry_hits(files):
    _, added = _new_lines(files, RETRY_PRESENCE)
    hits = [Hit("retry", path, l.new, f"{label} added: {_snippet(l.text)}") for label, path, l in added]

    removed, added = _new_lines(files, RETRY_NUMERIC)
    regexes = {label: regex for label, _, regex in RETRY_NUMERIC}
    old = collections.defaultdict(int)
    for label, path, l in removed:
        old[(label, path)] = max(old[(label, path)], _numeric_value(regexes[label], l.text))
    for label, path, l in added:
        value = _numeric_value(regexes[label], l.text)
        before = old[(label, path)]
        if value > before:
            change = f"raised from {before} to {value}" if before else f"set to {value}"
            hits.append(Hit("retry", path, l.new, f"{label} {change}: {_snippet(l.text)}"))
    return hits


# ---------------------------------------------------------------- gate config

COVERAGE_FILES = re.compile(r"^(\.coveragerc|\.nycrc(\..+)?|\.c8rc(\..+)?|\.?codecov\.ya?ml)$")
SONAR_FILES = {"sonar-project.properties", ".sonarcloud.properties"}
LINT_FILES = re.compile(
    r"^(\.eslintrc(\..+)?|eslint\.config\.[cm]?[jt]s|\.flake8|\.?pylintrc|\.?ruff\.toml|\.?clippy\.toml"
    r"|\.golangci\.(ya?ml|toml|json)|biome\.jsonc?|\.stylelintrc(\..+)?|stylelint\.config\.[cm]?js"
    r"|tslint\.json|\.?mypy\.ini|pyrightconfig\.json|\.rubocop\.ya?ml|detekt.*\.ya?ml|checkstyle.*\.xml)$")
COVERAGE_SECTIONS = ("tool.coverage", "coverage:")
LINT_SECTIONS = ("tool.ruff", "tool.pylint", "tool.flake8", "flake8", "pylint", "tool.mypy", "mypy",
                 "tool.pyright", "pycodestyle", "pydocstyle", "tool.pydocstyle", "tool.bandit", "bandit")
COVERAGE_LINE = re.compile(
    r"fail[_-]under|coverageThreshold|check-coverage|checkCoverage|jacocoTestCoverageVerification"
    r"|<minimum>|\bCOVEREDRATIO\b|\bMISSEDRATIO\b|\bMISSEDCOUNT\b|<haltOnFailure>", re.I)
JS_THRESHOLD = re.compile(r"^\s*[\"']?(?:lines|branches|functions|statements)[\"']?\s*:\s*-?\d|\bthresholds\s*:")
GRADLE_MINIMUM = re.compile(r"\bminimum\s*=")
LINT_DISABLE = re.compile(
    r"[\"']off[\"']|\boff\b|[:=]\s*[\"']?0[\"']?\s*,?\s*$|\bwarn(?:ing)?\b|\bignore|\bdisable|\ballow"
    r"|\bexclude|\bskip|\bfalse\b|\bnoqa\b", re.I)
BUDGET_KEYS = "maxAssetSize|maxEntrypointSize|maximumWarning|maximumError|maxNumericValue|chunkSizeWarningLimit"
# Generic key names count only in files that exist to hold budgets.
BUDGET_FILE_KEYS = "limit|budget|maxSize|maxBytes|max_bytes"
BUDGET_FILES = re.compile(
    r"^(\.size-limit.*|package\.json|\.?bundlewatch.*|\.bundlesizerc.*|budgets?\..*|\.?lighthouserc.*)$")
JS_CONFIG_JSON = re.compile(r"^(package\.json|.*(config|rc)\.jsonc?)$")
KEY_LINE = re.compile(r"^\s*[\"']?([\w.\-/@]+)[\"']?\s*[:=]")
SIZE_UNITS = {"": 1, "b": 1, "kb": 1e3, "kib": 1024, "mb": 1e6, "mib": 2 ** 20, "gb": 1e9,
              "gib": 2 ** 30, "ms": 1, "s": 1000}


def _budget_re(path):
    keys = BUDGET_KEYS + ("|" + BUDGET_FILE_KEYS if BUDGET_FILES.match(_name(path)) else "")
    return re.compile(r"[\"']?\b(" + keys + r")[\"']?\s*[:=]\s*[\"']?(-?\d+(?:\.\d+)?)\s*([A-Za-z]*)")


def _budget_value(number, unit):
    return float(number) * SIZE_UNITS.get(unit.lower(), 1)


def _lint_hits(fd, lines):
    """Lint config lines that disable a rule, or remove a setting outright.

    A line inside a multi-line value (an ignore list, say) is read together
    with the key line it continues.
    """
    keyed = []
    key = {"-": "", "+": ""}
    for l in lines:
        for side in ("-", "+"):
            if l.kind in (" ", side) and KEY_LINE.match(l.text):
                key[side] = l.text
        context = "" if KEY_LINE.match(l.text) else key["-" if l.kind == "-" else "+"]
        if l.kind != " " and l.text.strip() and not l.text.strip().startswith(("#", "//", ";")):
            keyed.append((l, bool(LINT_DISABLE.search(l.text + " " + context))))
    removed = [k for k in keyed if k[0].kind == "-"]
    added = [k for k in keyed if k[0].kind == "+"]
    removed, added = _cancel_pairs(removed, added, key=lambda k: k[0].text.strip().rstrip(","))
    # A key that is still set was changed, not removed; its new value is judged above.
    added_keys = {m.group(1) for m in (KEY_LINE.match(l.text) for l, _ in added) if m}
    hits = []
    for l, disables in added:
        if disables:
            hits.append(Hit("gate-config", fd.path, l.new, f"lint rule disabled: {_snippet(l.text)}"))
    for l, disables in removed:
        m = KEY_LINE.match(l.text)
        if m and m.group(1) in added_keys:
            continue
        if not disables and not l.text.strip().startswith(("[", "{", "}", "]")):
            hits.append(Hit("gate-config", fd.path, None, f"lint setting removed: {_snippet(l.text)}"))
    return hits


def _budget_hits(fd):
    regex = _budget_re(fd.path)
    removed, added = _cancel_pairs(fd.changed("-"), fd.changed("+"), key=lambda l: l.text.strip())
    old, new = collections.defaultdict(list), collections.defaultdict(list)
    for lines, out in ((removed, old), (added, new)):
        for l in lines:
            for m in regex.finditer(l.text):
                out[m.group(1)].append((_budget_value(m.group(2), m.group(3)), l))
    hits = []
    for key in sorted(set(old) | set(new)):
        for (before, _), (after, l) in zip(old[key], new[key]):
            if after > before:
                hits.append(Hit("gate-config", fd.path, l.new, f"budget raised: {_snippet(l.text)}"))
        if len(old[key]) > len(new[key]):
            hits.append(Hit("gate-config", fd.path, None, f"budget removed: {key}"))
    return hits


def gate_config_hits(files):
    hits = []
    for fd in files:
        paths = [p.lower() for p in fd.paths]
        names = {_name(p) for p in fd.paths}
        if any(p.startswith(".github/workflows/") for p in paths):
            hits.append(Hit("gate-config", fd.path, None, f"workflow {fd.status}"))
            continue
        if names & SONAR_FILES:
            hits.append(Hit("gate-config", fd.path, None, f"Sonar config {fd.status}"))
            continue
        if "codeowners" in names:
            hits.append(Hit("gate-config", fd.path, None, f"CODEOWNERS {fd.status}"))
            continue
        if any(COVERAGE_FILES.match(n) for n in names):
            hits.append(Hit("gate-config", fd.path, None, f"coverage config {fd.status}"))
            continue
        if any(is_doc(p) for p in fd.paths):
            continue
        if any(LINT_FILES.match(n) for n in names):
            hits.extend(_lint_hits(fd, fd.lines))
            continue

        sections = line_sections(fd) if is_ini(fd.path) else [""] * len(fd.lines)
        coverage, lint = [], []
        js_config = (_ext(fd.path) in JS_EXTS and is_config(fd.path)) or bool(JS_CONFIG_JSON.match(_name(fd.path)))
        gradle = ".gradle" in _name(fd.path)
        for l, section in zip(fd.lines, sections):
            if section.startswith(LINT_SECTIONS):
                lint.append(l)
            if l.kind == " ":
                continue
            if (section.startswith(COVERAGE_SECTIONS) or COVERAGE_LINE.search(l.text)
                    or (js_config and JS_THRESHOLD.search(l.text))
                    or (gradle and GRADLE_MINIMUM.search(l.text))):
                coverage.append(l)
        if coverage:
            first = coverage[0]
            more = f" (+{len(coverage) - 1} more)" if len(coverage) > 1 else ""
            hits.append(Hit("gate-config", fd.path, first.new,
                            f"coverage config edited: {_snippet(first.text)}{more}"))
        if lint:
            hits.extend(_lint_hits(fd, lint))
        if is_config(fd.path):
            hits.extend(_budget_hits(fd))
    return hits


# ------------------------------------------------------------------ reporting

def analyze(files):
    hits = removed_test_hits(files) + skip_hits(files) + retry_hits(files) + gate_config_hits(files)
    seen = set()
    unique = []
    for h in hits:
        # One line can match two rules of the same kind; report it once.
        if h.line is None or (h.rule, h.path, h.line) not in seen:
            seen.add((h.rule, h.path, h.line))
            unique.append(h)
    return sorted(unique, key=lambda h: (h.path, h.line or 0, h.rule, h.detail))


def _location(hit):
    return f"{hit.path}:{hit.line}" if hit.line else hit.path


def _cell(text):
    return text.replace("|", "\\|").replace("\n", " ")


def render_summary(hits, approved, note):
    out = ["### Gate guard", ""]
    if not hits:
        out.append("No removed tests, skip markers, retries or gate-config edits.")
        return "\n".join(out) + "\n"
    count = f"{len(hits)} hit{'s' if len(hits) != 1 else ''}"
    if approved:
        out.append(f"**Passing with {count}:** {note}.")
    else:
        out.append(f"**Failing with {count}:** {note}. The owner adds the `{APPROVAL_LABEL}` "
                   "label to approve them.")
    out += ["", "| Rule | Location | Detail |", "| --- | --- | --- |"]
    out += [f"| {h.rule} | `{_cell(_location(h))}` | {_cell(h.detail)} |" for h in hits]
    return "\n".join(out) + "\n"


def _escape_data(text):
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_property(text):
    return _escape_data(text).replace(":", "%3A").replace(",", "%2C")


def annotations(hits, approved):
    level = "warning" if approved else "error"
    out = []
    for h in hits[:MAX_ANNOTATIONS]:
        props = f"file={_escape_property(h.path)}"
        if h.line:
            props += f",line={h.line}"
        props += f",title={_escape_property('gate-guard ' + h.rule)}"
        out.append(f"::{level} {props}::{_escape_data(h.detail)}")
    return out


# ------------------------------------------------------------------- approval

def _next_link(header):
    for part in (header or "").split(","):
        url, _, rel = part.partition(";")
        if 'rel="next"' in rel:
            return url.strip()[1:-1]
    return None


def api_pages(api, path, token):
    """Every item of a paginated GitHub API list."""
    url = f"{api}{path}?per_page=100"
    items = []
    for _ in range(MAX_API_PAGES):
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "gate-guard"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as resp:
            items.extend(json.loads(resp.read().decode("utf-8")))
            url = _next_link(resp.headers.get("Link"))
        if not url:
            break
    return items


def label_approval(repo, pr, owner, require_actor, fetch):
    """(approved, reason) from the PR's labels and, if required, who applied the label."""
    labels = {l["name"].lower() for l in fetch(f"/repos/{repo}/issues/{pr}/labels")}
    if APPROVAL_LABEL not in labels:
        return False, f"the `{APPROVAL_LABEL}` label is not on this pull request"
    if not require_actor:
        return True, f"the `{APPROVAL_LABEL}` label is on this pull request"
    actor = None
    for event in fetch(f"/repos/{repo}/issues/{pr}/events"):
        label = (event.get("label") or {}).get("name", "")
        if event.get("event") == "labeled" and label.lower() == APPROVAL_LABEL:
            actor = (event.get("actor") or {}).get("login")
    if actor and actor.lower() == owner.lower():
        return True, f"the `{APPROVAL_LABEL}` label was applied by the owner @{actor}"
    return False, f"the `{APPROVAL_LABEL}` label was applied by @{actor or 'unknown'}, not the owner @{owner}"


def pr_from_event(path):
    if not path or not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        event = json.load(f)
    return (event.get("pull_request") or {}).get("number")


def approval(args):
    repo = args.repo or os.environ.get("GITHUB_REPOSITORY")
    pr = args.pr or pr_from_event(os.environ.get("GITHUB_EVENT_PATH"))
    if not repo or not pr:
        return False, "no pull request to read labels from"
    owner = args.owner or os.environ.get("GITHUB_REPOSITORY_OWNER") or repo.split("/")[0]
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    try:
        return label_approval(repo, pr, owner, args.require_owner_label_actor,
                              lambda path: api_pages(api, path, token))
    except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
        return False, f"could not read the pull request's labels ({e})"


# ----------------------------------------------------------------------- main

def read_diff(args):
    if args.diff_file:
        with open(args.diff_file, encoding="utf-8", errors="replace") as f:
            return f.read()
    # Full context lets ini/TOML edits be placed in their section.
    out = subprocess.run(
        ["git", "-C", args.repo_dir, "-c", "core.quotePath=false", "diff", "--no-color", "--no-ext-diff",
         "--no-textconv", "--find-renames", "--src-prefix=a/", "--dst-prefix=b/", "--unified=1000000",
         args.base, args.head],
        check=True, capture_output=True)
    return out.stdout.decode("utf-8", "replace")


def parse_args(argv):
    p = argparse.ArgumentParser(description="Flag pull request changes that loosen a repository's test gate.")
    p.add_argument("--diff-file", help="read the diff from this file instead of git")
    p.add_argument("--repo-dir", default=".", help="git checkout to diff (default: .)")
    p.add_argument("--base", default="HEAD^1", help="base revision (default: HEAD^1)")
    p.add_argument("--head", default="HEAD", help="head revision (default: HEAD)")
    p.add_argument("--repo", help="owner/name for the label lookup (default: $GITHUB_REPOSITORY)")
    p.add_argument("--pr", type=int, help="pull request number (default: from $GITHUB_EVENT_PATH)")
    p.add_argument("--owner", help="login that must apply the label (default: $GITHUB_REPOSITORY_OWNER)")
    p.add_argument("--require-owner-label-actor", action="store_true",
                   help=f"accept {APPROVAL_LABEL} only when the owner applied it")
    p.add_argument("--summary", default=os.environ.get("GITHUB_STEP_SUMMARY"),
                   help="append the Markdown report here (default: $GITHUB_STEP_SUMMARY)")
    return p.parse_args(argv)


def main(argv=None):
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure:
        reconfigure(errors="replace")
    args = parse_args(argv)
    try:
        diff = read_diff(args)
    except (OSError, subprocess.CalledProcessError) as e:
        detail = getattr(e, "stderr", b"") or b""
        print(f"gate-guard: cannot read the diff: {e} {detail.decode('utf-8', 'replace')}".strip(),
              file=sys.stderr)
        return 2
    hits = analyze(parse_diff(diff))
    approved, note = approval(args) if hits else (True, "")
    summary = render_summary(hits, approved, note)
    print(summary)
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as f:
            f.write(summary)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        for line in annotations(hits, approved):
            print(line)
    return 0 if approved else 1


if __name__ == "__main__":
    sys.exit(main())
