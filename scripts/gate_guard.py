#!/usr/bin/env python3
"""Flag pull request changes that loosen a repository's test gate.

Reads the pull request's unified diff and reports each hit:

    removed-tests  a test file deleted, or fewer test functions in a file
    skip-marker    a new skip, focus or expected-failure marker
    retry          a new or raised retry or rerun setting
    gate-config    an edit to workflows, Sonar or coverage config, a lint
                   rule disabled, or a size or performance budget raised

Moved lines are not hits: a test renamed or moved to another file, or a skip
marker that moves with its test, cancels out. A repository with a
platformio.ini or a C/C++ source or header fails closed instead: any change
that could change which tests run or what they include is a hit, whatever its
content (see c_cpp_hits and platformio_hits). Exit status is 0 when there are
no hits or the pull request carries the gate-change-approved label, 1 when
there are unapproved hits, and 2 when the diff cannot be read. Only the
standard library is used, and only the GitHub API is contacted, to read the
label when there are hits.

With --pull-request-target the script judges every open pull request whose
head is the head of a pull_request_target event, each against its current
base. It fetches the merge base and head commits as data, never checking out
or running them, and accepts an owner-only label (gate-change-approved,
findings-accepted) only when the owner applied it after the head commit was
pushed. Its exit status is the verdict, failing if any of those pull requests
fails, so the judging job's own check (gate-guard-target / gate-guard with the
caller template) is the one a ruleset requires. It posts nothing through the
Checks API: GitHub files a posted check run under a check suite it picks, which
a later run of the workflow can hide. The report goes to the job summary, and
one notice annotation states the verdict for pr_body_lint.py.

    python3 gate_guard.py --repo-dir . --base HEAD^1 --head HEAD
    python3 gate_guard.py --diff-file pr.diff
    python3 gate_guard.py --pull-request-target --guard-sha "$GUARD_SHA"
"""
import argparse
import ast
import base64
import bisect
import collections
import configparser
import datetime
import difflib
import io
import json
import os
import posixpath
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import tokenize
import urllib.error
import urllib.parse
import urllib.request
import warnings

APPROVAL_LABEL = "gate-change-approved"
FINDINGS_LABEL = "findings-accepted"
OWNER_LABELS = (APPROVAL_LABEL, FINDINGS_LABEL)
# The owner head record `ao owner-label apply` and approve_gate.py post, and
# the two things that mark a gate-change-approved label as approve_gate.py's:
# the owner's /approve-gate command, and the record approve_gate.py writes.
HEAD_RECORD = re.compile(r"<!-- owner-label (\S+) head=([0-9a-f]{40}) -->")
APPROVE_COMMAND = re.compile(r"\s*/approve-gate(?!\S)")
APPROVE_RECORD = re.compile(rf"Owner approved `{APPROVAL_LABEL}` at head [0-9a-f]{{40}} by \[comment\]")
# The judging job's check with the caller template: the caller's job id, then
# the reusable workflow's job name. pr_body_lint.py reads its verdict annotation.
REQUIRED_CHECK = "gate-guard-target / gate-guard"
VERDICT_TITLE = "gate-guard verdict"
# GitHub drops a step summary over 1 MiB; the guard stays a margin under it.
MAX_SUMMARY_BYTES = 1000000
TRUNCATED = "\n\n… (truncated; the job log has the rest)\n"
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
        # (old start, new start) of each hunk, whether git called the file binary,
        # the modes its header names (120000 is a symbolic link, 160000 a submodule),
        # and whether its mode changed.
        self.hunks = []
        self.binary = False
        self.modes = set()
        self.mode_changed = False

    @property
    def path(self):
        return self.old_path if self.status == "deleted" else self.new_path

    @property
    def paths(self):
        return [p for p in (self.old_path, self.new_path) if p]

    def changed(self, kind=None):
        return [l for l in self.lines if l.kind != " " and (kind is None or l.kind == kind)]


# ---------------------------------------------------------------- diff parsing

LINK_MODES = {"120000", "160000"}
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
        if line.startswith(("new file mode ", "deleted file mode ", "old mode ", "new mode ", "index ")):
            cur.modes.add(line.split()[-1])
        if line.startswith("old mode "):
            cur.mode_changed = True
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
        elif line.startswith(("Binary files ", "GIT binary patch")):
            cur.binary = True
        else:
            m = HUNK_RE.match(line)
            if m:
                old_no, new_no = int(m.group(1)), int(m.group(3))
                cur.hunks.append((old_no, new_no))
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
C_EXTS = {".c", ".cc", ".cpp", ".cxx"}
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
    if ext == ".rs" or ext in C_EXTS:
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
# Unity registers a test with RUN_TEST(name) and defines it as void test_name(void).
# Both are read from the whole file, once comments and literals are blanked, so
# a call may span lines and a line may hold several. The count only names what
# a change removed: any change to a test directory of a C/C++ repository is a
# hit anyway (see c_cpp_hits).
UNITY_RUN = re.compile(r"\bRUN_TEST\s*\(\s*(\w+)")
UNITY_DEF = re.compile(r"\bvoid\s+(test_\w*)\s*\(\s*(?:void\s*)?\)(?!\s*;)")
# A comment, or a raw, string or character literal. A quote after a word
# character is a digit separator, not a literal.
C_LEXEME = re.compile(
    r"//[^\n]*|/\*.*?(?:\*/|\Z)"
    r"|(?<!\w)(?:u8|[uUL])?R\"([^()\\\s]{0,16})\(.*?(?:\)\1\"|\Z)"
    r"|(?:(?<!\w)(?:u8|[uUL]))?\"(?:\\.|[^\"\\\n])*\"?"
    r"|(?<!\w)(?:u8|[uUL])?'(?:\\.|[^'\\\n])*'", re.S)
C_HEADER_EXTS = {".h", ".hh", ".hpp", ".hxx", ".inc", ".ipp"}
# Files that can be C/C++ sources or headers. A header needs no extension (<vector>).
C_FILE_EXTS = C_EXTS | C_HEADER_EXTS | {".c++", ".cp", ".h++", ".tpp", ".tcc", ".inl", ".ino", ".pde", ".s", ""}


def _line_defs(fd, regex, group):
    removed, added = [], []
    for l in fd.changed():
        m = regex.match(l.text)
        if m:
            (removed if l.kind == "-" else added).append((m.group(group), l.old or l.new))
    return removed, added


def _c_blank(text):
    """text with lines spliced as the C preprocessor splices them, and comments and literals blanked."""
    text = text.replace("\\\n", "")
    out, pos = [], 0
    for m in C_LEXEME.finditer(text):
        lines = "\n" * m.group(0).count("\n")
        out += [text[pos:m.start()], " " + lines if m.group(0).startswith(("//", "/*")) else '""' + lines]
        pos = m.end()
    return "".join(out + [text[pos:]])


def _unity_items(text):
    """(name, line, kind) for each RUN_TEST registration ("run") and test definition ("def") in one side of a file."""
    blank = _c_blank(text)
    starts = [0] + [i + 1 for i, c in enumerate(blank) if c == "\n"]
    items = [(m.group(1), bisect.bisect_right(starts, m.start()), kind)
             for kind, regex in (("run", UNITY_RUN), ("def", UNITY_DEF)) for m in regex.finditer(blank)]
    return sorted(items, key=lambda d: d[1])


def _whole_sides(fd):
    """(old text, new text) of fd, or None when the diff does not show the whole file.

    git_diff asks for full context, so a changed file is one hunk from line 1.
    A file with no hunk is empty, or renamed or re-moded without a change.
    """
    if fd.binary or len(fd.hunks) > 1 or any(start > 1 for hunk in fd.hunks for start in hunk):
        return None
    return _side_text(fd, "-"), _side_text(fd, "+")


def _side_text(fd, side):
    """One side of a file, rebuilt from the diff, which carries the whole file (git_diff asks for full context)."""
    return "".join(l.text + "\n" for l in fd.lines if l.kind in (" ", side))


def _unity_defs(fd):
    """Unity registrations and definitions one side of the whole file has more of than the other.

    The kind keeps an edit to a definition from cancelling the removal of its
    registration; removed_test_hits counts a test's two kinds once, by name.
    """
    sides = _whole_sides(fd)
    if sides is None:
        return [], []
    return _cancel_pairs(*(_unity_items(text) for text in sides), key=_def_key)


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
    if ext in C_EXTS | C_HEADER_EXTS and any(_in_test_dir(p) for p in fd.paths):
        return _unity_defs(fd)
    return [], []


def _names(defs, limit=5):
    names = [d[0] if d[0] is not None else f"test at line {d[1]}" for d in defs]
    return ", ".join(names[:limit]) + (f" and {len(names) - limit} more" if len(names) > limit else "")


def _def_key(d):
    """Identity of a definition in its file: its name, and for Unity whether it is
    a registration or a definition."""
    return (d[0], *d[2:3])


def _by_name(defs):
    """Unity items named once, a test's registration and definition together."""
    seen = set()
    out = []
    for d in defs:
        if len(d) > 2:
            if d[0] in seen:
                continue
            seen.add(d[0])
        out.append(d)
    return out


def _decrease(removed, added):
    """How many fewer tests a file has. Unity registrations and definitions are
    counted apart, so a new test that is never registered does not offset a
    registration that was removed, and a test removed whole counts once."""
    if not any(len(d) > 2 for d in removed + added):
        return len(removed) - len(added)

    def fewer(kind):
        return sum(d[2] == kind for d in removed) - sum(d[2] == kind for d in added)
    return max(fewer("run"), fewer("def"), 0)


def removed_test_hits(files):
    """Deleted test files and per-file decreases, after pairing renames and moves."""
    left = {}
    for fd in files:
        removed, added = test_defs(fd)
        # A test removed and re-added in the same file was edited, not removed.
        left[id(fd)] = list(_cancel_pairs(removed, added, key=_def_key))
    # A test removed from one file and added to another was moved.
    pool = collections.defaultdict(list)
    for fd in files:
        for d in left[id(fd)][1]:
            if d[0] is not None:
                pool[_def_key(d)].append((id(fd), d))
    for fd in files:
        removed = left[id(fd)][0]
        kept = []
        for d in removed:
            match = next((i for i, (owner, _) in enumerate(pool.get(_def_key(d), [])) if owner != id(fd)), None)
            if match is None:
                kept.append(d)
                continue
            owner, added = pool[_def_key(d)].pop(match)
            left[owner][1].remove(added)
        left[id(fd)][0] = kept

    hits = []
    for fd in files:
        decrease = _decrease(*left[id(fd)])
        removed, added = map(_by_name, left[id(fd)])
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


# ------------------------------------------------------------ C/C++ and PlatformIO
#
# The guard does not model the C preprocessor or PlatformIO's config: ten
# review rounds each found another way a model missed a removed test. In a
# repository with a platformio.ini or a C/C++ source or header in either tree,
# it stays quiet only when a pull request cannot change which tests run or
# what they include. Anything else is a hit, whatever its content:
#
#   - a change to any platformio.ini (skip-marker, see platformio_hits);
#   - a change to any file in or under a test directory, in either tree,
#     except a Python file in a test directory that holds no C/C++ source or
#     header in either tree (a Python backend's tests/), when its change only
#     replaces compared literals and comparison operators in assert statements
#     of its existing tests (issue #40, see _python_unexempt). The guard does
#     not model what pytest collects or skips, so any other change to such a
#     file is a hit;
#   - an added, deleted or renamed C/C++ source or header anywhere, or a
#     changed submodule;
#   - every pull request, when a platformio.ini names extra_configs or test_dir
#     (any file may be a config it loads or a test it runs), cannot be read, or
#     the trees hold a symbolic link or submodule at or in a test directory.

def _touched(fd):
    """Whether the diff changes fd at all: its content, mode, path or existence."""
    return bool(fd.changed()) or fd.status != "modified" or fd.binary or fd.mode_changed


def _at_test_dir(path):
    """Whether path is a test directory or is in one."""
    return any(d in TEST_DIRS for d in path.lower().split("/"))


def _test_root(path):
    """The outermost test directory path is in, lower-cased, or None."""
    parts = path.lower().split("/")[:-1]
    return next(("/".join(parts[:i + 1]) for i, d in enumerate(parts) if d in TEST_DIRS), None)


def _python_test(path, facts):
    """Whether path is a Python file outside test directories, or in one that holds no C/C++ source or header."""
    return _ext(path) in PY_EXTS and _test_root(path) not in facts.c_test_roots


# A source encoding declaration (PEP 263). The guard reads a file as UTF-8, so a
# file that may declare another, on any line, is not exempt.
PY_CODING = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*([-\w.]+)")
# What a changed line may hold: printable ASCII. Python reads some other letters
# as ASCII ones, and tabs and form feeds as indentation.
PY_PRINTABLE = re.compile(r"[ -~]*")
PY_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)
# The nodes ast.parse gives a literal, by name: Constant from Python 3.8; Num,
# Str, Bytes, NameConstant and Ellipsis on 3.7, which 3.14 no longer has.
PY_LITERALS = frozenset(("Constant", "Num", "Str", "Bytes", "NameConstant", "Ellipsis"))
# Nodes a changed assertion may not hold, by name: they decide whether or how
# often its other code runs (and, or, a conditional, a comprehension, a
# lambda), bind a name (:=), suspend the test, or unpack an iterable.
PY_UNSAFE = frozenset(("BoolOp", "IfExp", "ListComp", "SetComp", "DictComp", "GeneratorExp", "Lambda", "NamedExpr",
                       "Await", "Yield", "YieldFrom", "Starred"))
# pytest.approx(expected, rel, abs): the arguments an exempt change may give another literal.
PY_TOLERANCES = ("rel", "abs")


def _py_chain(node):
    """node as a dotted name, such as os.path.join, or None when it is not one."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    return ".".join([node.id] + parts[::-1]) if isinstance(node, ast.Name) else None


def _py_marks_only(node):
    """Whether every decorator of node is a pytest mark, which leaves it collected."""
    return all((_py_chain(d.func if isinstance(d, ast.Call) else d) or "").startswith(("pytest.mark.", "mark."))
               for d in node.decorator_list)


def _python_tests(tree):
    """The tests pytest collects from a module, by name: top-level test_ functions, and test_ methods of
    top-level Test classes that have no constructor, with no decorator but pytest marks."""
    tests = []
    for node in tree.body:
        if isinstance(node, PY_DEFS) and node.name.startswith("test_") and _py_marks_only(node):
            tests.append(node)
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test") and _py_marks_only(node):
            methods = [n for n in node.body if isinstance(n, PY_DEFS) and n.name.startswith("test_")
                       and _py_marks_only(n)]
            rest = [n for n in node.body if n not in methods]
            if not any(getattr(n, "name", getattr(n, "id", None)) in ("__init__", "__new__")
                       for statement in rest for n in ast.walk(statement)):
                tests += methods
    return tests


def _py_coding(lines):
    """The encoding a line of lines declares that is not UTF-8, or None."""
    names = (m.group(1) for m in map(PY_CODING.match, lines) if m)
    return next((n for n in names if n.lower().replace("_", "-") not in ("utf-8", "utf8")), None)


def _py_start(node):
    """A statement's first line: its first decorator's, if it has any (Python 3.7 gives that line, later
    versions the def or class line)."""
    return min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])


def _py_body_lines(tree, tests, lines):
    """The numbers of the lines of lines, the text of tree, inside the body of one of tests.

    A test reaches from its first line to the line before the next statement
    outside it. Its body's lines are those indented deeper than its def, and
    blank lines between two of them: indentation, not the line numbers of the
    body's statements, which Python 3.7 gives as a multi-line string's last.
    """
    statements = [n for n in ast.walk(tree) if isinstance(n, ast.stmt)]
    inside = set()
    for test in tests:
        own, start = set(ast.walk(test)), _py_start(test)
        end = min((_py_start(n) for n in statements if n not in own and _py_start(n) > start),
                  default=len(lines) + 1)
        deeper = [i for i in range(start, end) if lines[i - 1].strip(" \t\f")
                  and len(lines[i - 1]) - len(lines[i - 1].lstrip(" \t\f")) > test.col_offset]
        if deeper:
            inside.update(i for i in range(deeper[0], deeper[-1] + 1)
                          if i in deeper or not lines[i - 1].strip(" \t\f"))
    return inside


def _py_binds_here(node):
    """The names node itself binds or declares, not those of the nodes inside it, with '*' for a wildcard import."""
    if isinstance(node, ast.Name):
        return set() if isinstance(node.ctx, ast.Load) else {node.id}
    if isinstance(node, ast.arg):
        return {node.arg}
    if isinstance(node, ast.alias):
        return {(node.asname or node.name).split(".")[0]}
    if isinstance(node, (ast.Global, ast.Nonlocal)):
        return set(node.names)
    return {v for f, v in ast.iter_fields(node) if f in ("name", "rest") and isinstance(v, str)}


def _py_pytest(tree):
    """Whether pytest in tree is the pytest module: one top-level import pytest is all that binds or declares
    the name, nothing assigns or deletes an attribute of it, and no wildcard import may rebind it."""
    imports = [a for n in tree.body if isinstance(n, ast.Import)
               for a in n.names if a.name == "pytest" and not a.asname]
    for n in ast.walk(tree):
        if n not in imports and {"pytest", "*"} & _py_binds_here(n):
            return False
        if (isinstance(n, ast.Attribute) and not isinstance(n.ctx, ast.Load)
                and (_py_chain(n) or "").startswith("pytest.")):
            return False
    return len(imports) == 1


def _py_text(node):
    """Whether node is a string literal."""
    return type(node).__name__ == "Str" or (type(node).__name__ == "Constant" and isinstance(node.value, str))


def _py_number(node):
    """Whether node is an int or float literal, not a bool or a complex number (Num on Python 3.7)."""
    if type(node).__name__ not in ("Constant", "Num"):
        return False
    return type(node.value if hasattr(node, "value") else node.n) in (int, float)


def _py_leaves(node):
    """The ids of the literals in node, a literal (see _py_masks), or None when it is not one.

    A container's own node is not one of them, so its kind and length stay
    compared; a minus on an int or float counts as part of the literal, so 5
    may become -5, but no other unary operator or operand does.
    """
    if type(node).__name__ in PY_LITERALS:
        return {id(node)}
    if isinstance(node, ast.UnaryOp):
        return {id(node)} if isinstance(node.op, ast.USub) and _py_number(node.operand) else None
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        parts = node.elts
    elif isinstance(node, ast.Dict):
        parts = node.keys + node.values
    else:
        return None
    leaves = [_py_leaves(p) if p is not None else None for p in parts]
    return None if None in leaves else set().union(*leaves)


def _py_masks(node, approx):
    """The ids of the nodes of the assert statement node an exempt change may give other literals.

    They are its message if that is a string, and, where its test is a
    comparison, the literals of each literal operand (a constant, a negated
    number, or a list, tuple, set or dict of literals) and, when approx says
    pytest is the pytest module, those of a pytest.approx operand's expected
    value and its rel and abs.
    """
    masked = {id(node.msg)} if node.msg is not None and _py_text(node.msg) else set()
    if not isinstance(node.test, ast.Compare):
        return masked
    for operand in [node.test.left] + node.test.comparators:
        literal = [operand]
        if approx and isinstance(operand, ast.Call) and _py_chain(operand.func) == "pytest.approx":
            literal = operand.args[:3] + [k.value for k in operand.keywords if k.arg in PY_TOLERANCES]
        for part in literal:
            masked |= _py_leaves(part) or set()
    return masked


def _py_masked(node, masked):
    """node as text without line numbers, with each node whose id is in masked, and each comparison
    operator, as a placeholder."""
    if isinstance(node, list):
        return "[" + ", ".join(_py_masked(n, masked) for n in node) + "]"
    if id(node) in masked:
        return "<literal>"
    if isinstance(node, ast.cmpop):
        return "<comparison>"
    if not isinstance(node, ast.AST):
        return repr(node)
    return f"{type(node).__name__}({', '.join(f'{f}={_py_masked(v, masked)}' for f, v in ast.iter_fields(node))})"


def _py_assert_change(old, new, name, approx):
    """Why changing the assert statement old of the test name to new is more than replacing its compared
    literals, comparison operators and pytest.approx tolerances, or None."""
    for node in (old, new):
        if any(type(n).__name__ in PY_UNSAFE for n in ast.walk(node)):
            return (f"it changes an assertion in {name} that holds an and, or, a conditional expression, a "
                    "comprehension, a lambda, :=, await, yield or a starred item, which decide what else it runs")
        if node.msg is not None and not _py_text(node.msg):
            return (f"it changes an assertion in {name} whose message is not a plain string; the message runs only "
                    "when the assertion fails")
        test = node.test
        if isinstance(test, ast.Compare) and len(test.ops) > 1 and not all(
                isinstance(o, ast.Name) or _py_leaves(o) for o in [test.left] + test.comparators):
            return (f"it changes a chained comparison in {name} with an operand that is not a name or a literal, "
                    "which runs only while the links before it hold")
    if _py_masked(old, _py_masks(old, approx)) != _py_masked(new, _py_masks(new, approx)):
        rebound = "" if approx or "pytest.approx" not in {_py_chain(n) for n in ast.walk(new)} else (
            "; pytest.approx counts only where a top-level import pytest is all that binds pytest")
        return (f"it changes an assertion in {name} outside a comparison's literal operands, its operators and a "
                f"pytest.approx tolerance: a name, an attribute, a call or its argument, or the expression's "
                f"shape{rebound}")
    return None


def _python_test_change(old, new, approx):
    """Why changing the test old to new is more than replacing assert statements of its own body, each by one
    that differs only in compared literals, comparison operators and pytest.approx tolerances, or None."""
    dumps = [[ast.dump(s) for s in test.body] for test in (old, new)]
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, *dumps, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        if i2 - i1 != j2 - j1:
            return f"it adds, removes or moves a statement in {new.name}"
        for a, b in zip(old.body[i1:i2], new.body[j1:j2]):
            if not (isinstance(a, ast.Assert) and isinstance(b, ast.Assert)):
                return (f"it changes a statement of {new.name} that is not an assert statement of a test's own "
                        "body: a plain statement, a docstring, or an if, for, while, with or try, even around an "
                        "assert")
            why = _py_assert_change(a, b, new.name, approx)
            if why:
                return why
    return None


def _py_logical_lines(text):
    """{line number: (first, last)} of each line of text in a logical line, which blank and comment lines
    are not."""
    found, first = {}, None
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type in (tokenize.COMMENT, tokenize.NL, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER):
            continue
        first = token.start[0] if first is None else first
        if token.type == tokenize.NEWLINE:
            found.update((i, (first, token.end[0])) for i in range(first, token.end[0] + 1))
            first = None
    return found


def _py_line_change(tree, tests, text, changed):
    """Why changed, the numbers of the changed lines of text, parsed as tree with its tests, are more than the
    lines of assert statements of the tests' own bodies and comments and blank lines inside them, or None."""
    lines, logical = text.split("\n")[:-1], _py_logical_lines(text)
    asserts = {s.lineno for test in tests for s in test.body if isinstance(s, ast.Assert)}
    starts = collections.Counter(n.lineno for n in ast.walk(tree) if isinstance(n, ast.stmt))
    bodies = _py_body_lines(tree, tests, lines)
    for i in sorted(changed):
        if i not in logical:
            if i not in bodies:
                return "it changes a comment or a blank line outside a test body"
            continue
        first, last = logical[i]
        if first not in asserts:
            return "it changes a line outside an assert statement of a test's own body"
        if sum(starts[j] for j in range(first, last + 1)) != 1:
            return "an assert statement it changes shares its line with other code"
    return None


def _python_unexempt(fd):
    """Why fd, a Python file in a Python test directory of a C/C++ repository, is not exempt, or None.

    Issue #40, the owner's assert-only rule: only an edit in place of a
    test_*.py or *_test.py module that replaces assert statements of its
    existing tests' own bodies is exempt, each new assert the removed one
    with only its compared literals, comparison operators and pytest.approx
    tolerances changed (_python_test_change). Every other line of the file
    stays as it was, but for comments and blank lines inside a test body.
    Every changed line holds only printable ASCII, and the file must be
    UTF-8 as Python reads it, so the text the guard compares is the code
    Python runs.
    """
    if not is_test_module(fd.path):
        return "it is not a test_*.py or *_test.py module (a conftest.py or a helper can change any test)"
    if fd.status != "modified" or fd.mode_changed:
        return "only an edit in place can be exempt, not a rename, an addition, a deletion or a mode change"
    sides = _whole_sides(fd)
    if sides is None:
        return "the diff does not show the whole file"
    if any("\r" in text for text in sides):
        return ("it holds a carriage return inside a line, which Python reads as a line break and git does not, "
                "so the guard cannot tell which lines are inside a test")
    coding = _py_coding(sides[0].split("\n") + sides[1].split("\n"))
    if coding:
        return f"it declares the source encoding {coding}; the guard reads only UTF-8"
    if not all(PY_PRINTABLE.fullmatch(l.text) for l in fd.lines if l.kind != " "):
        return ("a changed line holds a character that is not printable ASCII, such as a tab, a form feed or a "
                "letter Python reads as an ASCII one")
    texts = [text.lstrip("﻿") for text in sides]
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            trees = [ast.parse(text) for text in texts]
            for text in texts:
                _py_logical_lines(text)
    except (SyntaxError, ValueError, tokenize.TokenError):
        return "the guard cannot parse it"
    tests, skeletons = [_python_tests(tree) for tree in trees], []
    for tree, side in zip(trees, tests):
        bodies = [t.body for t in side]
        for t in side:
            t.body = []
        skeletons.append(ast.dump(tree))
        for t, body in zip(side, bodies):
            t.body = body
    if skeletons[0] != skeletons[1]:
        return ("it changes code outside a test body: a def or class line, a decorator, an import, or module- or "
                "class-level code")
    approx = all(_py_pytest(tree) for tree in trees)
    why = next(filter(None, (_python_test_change(old, new, approx) for old, new in zip(*tests))), None)
    changed = [{l.old for l in fd.lines if l.kind == "-"}, {l.new for l in fd.lines if l.kind == "+"}]
    return why or next(filter(None, map(_py_line_change, trees, tests, texts, changed)), None)


def c_cpp_hits(files, facts, flagged):
    """removed-tests hits for a C/C++ repository's changes that may change which tests run or what they include.

    flagged holds the paths that already have a removed-tests hit.
    """
    if not facts.c_cpp:
        return []
    hits = [Hit("removed-tests", path, None, "a symbolic link or submodule at or in a test directory; the guard "
                "does not follow it, so any change may change the tests it holds")
            for path in facts.blind] if any(_touched(fd) for fd in files) else []
    for fd in files:
        if not _touched(fd) or fd.path in flagged or fd.path in facts.blind:
            continue
        if any(_at_test_dir(p) for p in fd.paths):
            if not all(_python_test(p, facts) for p in fd.paths):
                detail = f"{fd.status} a file in a test directory of a C/C++ repository"
            else:
                why = _python_unexempt(fd)
                if why:
                    hits.append(Hit("removed-tests", fd.path, None, f"{fd.status} a Python test file of a C/C++ "
                                    f"repository: {why}; only replacing compared literals or comparison operators in "
                                    "assert statements of existing tests is exempt, so this needs approval"))
                continue
        elif fd.modes & {"160000"}:
            detail = f"{fd.status} a submodule of a C/C++ repository, which may hold sources or headers"
        elif fd.status != "modified" and any(_ext(p) in C_FILE_EXTS for p in fd.paths):
            detail = f"{fd.status} a C/C++ source or header, which can change what tests include"
        else:
            continue
        hits.append(Hit("removed-tests", fd.path, None, detail + "; the guard does not read C/C++ tests, so this "
                        "needs approval whatever the change"))
    return hits


# --------------------------------------------------------------- skip markers

def _py_tests(path):
    return _ext(path) in PY_EXTS and is_test_file(path)


def _js_tests(path):
    return _ext(path) in JS_EXTS and is_test_file(path)


# The test_* and extends options a platformio.ini hit names. Option names
# ignore case, as ConfigParser lowers them; section names keep theirs.
PIO_TEST_OPTION = re.compile(r"^(?:test_\w*|extends)$")

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


def _ini_options(text):
    """{section: {option: value as written}} for one side of an ini file, [DEFAULT] as a section of its own.

    PlatformIO parses with ConfigParser(inline_comment_prefixes=("#", ";")).
    """
    parser = configparser.ConfigParser(inline_comment_prefixes=("#", ";"), interpolation=None, default_section="\0")
    parser.read_string(text)
    return {s: dict(parser.items(s)) for s in parser.sections()}


def _pio_value(value):
    return _snippet(" ".join(value.split()), 60) or "(empty)"


def _pio_details(fd):
    """What a change to a platformio.ini does, as hit details. Any change is a hit:
    the details name each test_* or extends option it changes, when the guard can
    read both sides, and otherwise say that the file changed."""
    sides = _whole_sides(fd)
    details = []
    try:
        old, new = (_ini_options(text) for text in sides) if sides else ({}, {})
    except configparser.Error:
        old = new = {}
    for section in dict.fromkeys([*new, *old]):
        before, after = old.get(section, {}), new.get(section, {})
        for option in dict.fromkeys([*after, *before]):
            if not PIO_TEST_OPTION.match(option) or before.get(option) == after.get(option):
                continue
            if option not in before:
                change = f"added: {_pio_value(after[option])}"
            elif option not in after:
                change = f"removed, was: {_pio_value(before[option])}"
            else:
                change = f"changed: {_pio_value(before[option])} -> {_pio_value(after[option])}"
            details.append(f"PlatformIO [{section}] {option} {change}")
    generic = (f"PlatformIO {fd.path} {fd.status}; the guard does not read PlatformIO's config, so any change to it "
               "needs approval")
    return details or [generic]


def platformio_hits(files, facts):
    """skip-marker hits for every changed platformio.ini, and for every platformio.ini in either
    tree whose extra_configs or test_dir the guard does not follow, or that it cannot read."""
    hits, changed = [], set()
    for fd in files:
        if _touched(fd) and any(_name(p) == "platformio.ini" for p in fd.paths):
            hits += [Hit("skip-marker", fd.path, None, d) for d in _pio_details(fd)]
            changed.update(fd.paths)
    if any(_touched(fd) for fd in files):
        hits += [Hit("skip-marker", path, None, f"PlatformIO {path} {why}, so any changed file may be a config "
                     "PlatformIO loads or a test it runs")
                 for path, why in facts.open_configs if path not in changed]
    return hits


def _new_lines(files, rules, imperative_rules=()):
    """Added matches that are not a moved copy of a removed match anywhere in the diff."""
    found = _matching_lines(files, rules) + _matching_lines(files, imperative_rules, imperative=True)
    removed = [f for f in found if f[2].kind == "-"]
    added = [f for f in found if f[2].kind == "+"]
    return _cancel_pairs(removed, added, key=lambda f: f[2].text.strip())


def skip_hits(files, facts=None):
    _, added = _new_lines(files, SKIP_RULES, IMPERATIVE_SKIP_RULES)
    return ([Hit("skip-marker", path, l.new, f"{label}: {_snippet(l.text)}") for label, path, l in added]
            + platformio_hits(files, facts or tree_facts(DiffTrees(files))))


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


# ---------------------------------------------------------------------- trees
#
# The C/C++ and PlatformIO rules read the trees beyond the diff: which paths
# exist, which are links or submodules, and the text of every platformio.ini.
# Each side is "-" (base) or "+" (head).

class DiffTrees:
    """The files the diff shows, as the trees of a --diff-file run; a file it does not show whole cannot be read."""

    def __init__(self, files):
        self.files = {"-": {}, "+": {}}
        self.link_paths = [p for fd in files if fd.modes & LINK_MODES for p in fd.paths]
        for fd in files:
            sides = _whole_sides(fd)
            for side, path, i in (("-", fd.old_path, 0), ("+", fd.new_path, 1)):
                if path:
                    self.files[side][path] = None if sides is None or fd.modes & LINK_MODES else sides[i]

    def paths(self, side):
        return list(self.files[side])

    def links(self, side):
        return self.link_paths

    def read(self, side, path):
        return self.files[side].get(path)


class GitTrees:
    """The base and head trees of a git repository, read as data: nothing is checked out or run."""

    def __init__(self, repo_dir, base, head):
        self.repo_dir, self.revs = repo_dir, {"-": base, "+": head}
        self.trees, self.link_paths, self.blobs = {}, {}, {}

    def _git(self, *args):
        return subprocess.run(["git", "-C", self.repo_dir, *args], check=True, capture_output=True).stdout

    def _tree(self, side):
        if side not in self.trees:
            tree, links = {}, []
            for entry in self._git("ls-tree", "-r", "-z", "--full-tree", self.revs[side]).split(b"\0"):
                meta, _, raw = entry.partition(b"\t")
                mode, kind, sha = (meta.decode("ascii").split() + ["", "", ""])[:3]
                path = raw.decode("utf-8", "replace")
                if mode in LINK_MODES:
                    # A symbolic link's blob is its target, and a submodule's files are not in the tree.
                    links.append(path)
                if kind == "blob":
                    tree[path] = None if mode in LINK_MODES else sha
            self.trees[side], self.link_paths[side] = tree, links
        return self.trees[side]

    def paths(self, side):
        return list(self._tree(side))

    def links(self, side):
        self._tree(side)
        return self.link_paths[side]

    def read(self, side, path):
        sha = self._tree(side).get(path)
        if sha is None:
            return None
        if sha not in self.blobs:
            self.blobs[sha] = self._git("cat-file", "blob", sha)
        try:
            return self.blobs[sha].decode("utf-8")
        except UnicodeDecodeError:
            return None


# c_cpp: whether either tree has a platformio.ini or a C/C++ source or header;
# open_configs: [(platformio.ini, why)] for those that name extra_configs or
# test_dir, or that cannot be read; blind: symbolic links and submodules at or
# in a test directory, in a C/C++ repository; c_test_roots: the outermost test
# directories (see _test_root) that hold a C/C++ source or header in either
# tree, counting any file with no extension but git's own.
TreeFacts = collections.namedtuple("TreeFacts", "c_cpp open_configs blind c_test_roots")
PIO_OPEN_OPTION = re.compile(r"extra_configs|test_dir", re.IGNORECASE)
GIT_FILES = {".gitignore", ".gitattributes", ".gitkeep", ".keep"}


def tree_facts(trees):
    """The TreeFacts of a base and head tree; see TreeFacts. Nothing is parsed: a
    platformio.ini that mentions extra_configs or test_dir anywhere, even in a
    comment, is open."""
    paths = {(side, path) for side in ("-", "+") for path in trees.paths(side)}
    c_cpp = any(_name(p) == "platformio.ini" or _ext(p) in C_EXTS | {".h", ".hh", ".hpp", ".hxx"} for _, p in paths)
    open_configs = {}
    for side, path in sorted(paths):
        if _name(path) != "platformio.ini" or path in open_configs:
            continue
        text = trees.read(side, path)
        if text is None:
            open_configs[path] = "cannot be read (binary, not UTF-8 or a symbolic link)"
        elif PIO_OPEN_OPTION.search(text):
            open_configs[path] = f"names {' and '.join(sorted({m.lower() for m in PIO_OPEN_OPTION.findall(text)}))}"
    links = dict.fromkeys(link for side in ("-", "+") for link in trees.links(side))
    blind = [link for link in links if _at_test_dir(link)] if c_cpp else []
    c_test_roots = {_test_root(p) for _, p in paths if _ext(p) in C_FILE_EXTS and _name(p) not in GIT_FILES} - {None}
    return TreeFacts(c_cpp, sorted(open_configs.items()), blind, c_test_roots)


def repo_hits(repo_dir, base, head):
    """The hits from base to head of a git repository, reading its trees beyond the diff."""
    return analyze(parse_diff(git_diff(repo_dir, base, head)), tree_facts(GitTrees(repo_dir, base, head)))


# ------------------------------------------------------------------ reporting

def analyze(files, facts=None):
    """The hits of a diff. facts come from the base and head trees; without them, from the files the diff shows."""
    facts = facts or tree_facts(DiffTrees(files))
    removed = removed_test_hits(files)
    hits = (removed + c_cpp_hits(files, facts, {h.path for h in removed}) + skip_hits(files, facts)
            + retry_hits(files) + gate_config_hits(files))
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


def render_summary(hits, approved, note, title="Gate guard"):
    out = [f"### {title}", ""]
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


def api_request(url, token, method="GET", body=None):
    """(parsed JSON, Link header) of one GitHub API call."""
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": "gate-guard"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8") or "null"), resp.headers.get("Link")


class IncompleteList(ValueError):
    """A paginated list still had pages left when api_pages stopped reading."""


def api_pages(api, path, token):
    """Every item of a paginated GitHub API list.

    Raises IncompleteList rather than return part of a list: label events come
    oldest first, so a cut-off history would date a label by a stale event.
    """
    url = f"{api}{path}{'&' if '?' in path else '?'}per_page=100"
    items = []
    for _ in range(MAX_API_PAGES):
        page, link = api_request(url, token)
        items.extend(page)
        url = _next_link(link)
        if not url:
            return items
    raise IncompleteList(f"{path} has more than {MAX_API_PAGES} pages; the guard reads no partial list")


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


def load_event(path):
    if not path or not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def pr_from_event(path):
    return (load_event(path).get("pull_request") or {}).get("number")


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


# -------------------------------------------------------- pull_request_target

def parse_time(text):
    """An aware datetime from a GitHub ISO 8601 timestamp, or None."""
    try:
        return datetime.datetime.fromisoformat((text or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def _stamp(when):
    return when.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def head_pushed_at(activities, ref, sha):
    """When sha last became the tip of ref, from the repository activity API, or None.

    That is GitHub's own record of the push. A commit's author and committer
    dates are whatever its maker wrote, so they cannot date a push.
    """
    times = [parse_time(a.get("timestamp")) for a in activities
             if a.get("after") == sha and a.get("ref") == ref]
    times = [t for t in times if t]
    return max(times) if times else None


def _by(comment, owner):
    return ((comment.get("user") or {}).get("login") or "").lower() == owner.lower()


def from_approve_gate(comments, owner, applied):
    """Whether the owner's gate-change-approved label applied at `applied` may be approve_gate.py's.

    The workflow labels with the owner's token, so the event's actor can't
    tell. It labels only after the owner's /approve-gate comment, and records
    the head after the label. Either one, from the owner and no newer than the
    label, marks it. An edited command still marks it: the edit may postdate
    the run. A time that can't be read marks it too.
    """
    for c in comments:
        if not _by(c, owner):
            continue
        body, at = c.get("body") or "", parse_time(c.get("created_at"))
        if (APPROVE_COMMAND.match(body) or APPROVE_RECORD.search(body)) and (at is None or at <= applied):
            return True
    return False


def recorded_head(comments, label, owner, since):
    """(sha, posted) from the owner's newest unedited head record for label posted at or after since, or None.

    Anyone with write access can edit the owner's comment, so an edited one
    never counts (the rule of agent-ops `ao owner-label`).
    """
    found = None
    for c in comments:
        at = parse_time(c.get("created_at"))
        if not _by(c, owner) or at is None or at < since or c.get("updated_at") != c.get("created_at"):
            continue
        for match in HEAD_RECORD.finditer(c.get("body") or ""):
            if match.group(1).lower() == label and (found is None or at >= found[1]):
                found = (match.group(2), at)
    return found


def owner_label(label, labels, events, owner, sha, pushed_at, comments=()):
    """(counts, reason) for an owner-only label on a pull request whose head is sha.

    The label counts only when the newest labeled/unlabeled event for it is
    'labeled' by the owner, after sha was pushed. A label a worker re-applies
    becomes the worker's, and a new push makes the owner's label stale.

    A gate-change-approved label approve_gate.py may have applied also needs
    the owner's newest head record since the label, among the pull request's
    comments, to name sha. The workflow re-reads the head after it labels, but
    a run that dies, or an API that fails, before the record or the re-read
    can leave its label on a head pushed in between.
    """
    if label not in labels:
        return False, f"`{label}` is not on this pull request"
    history = [e for e in events if e.get("event") in ("labeled", "unlabeled")
               and ((e.get("label") or {}).get("name") or "").lower() == label]
    if not history:
        return False, f"no labeled event was found for `{label}`"
    newest = max(history, key=lambda e: (e.get("created_at") or "", e.get("id") or 0))
    actor = (newest.get("actor") or {}).get("login") or "unknown"
    if newest.get("event") != "labeled":
        return False, f"`{label}` was last removed, by @{actor}"
    if actor.lower() != owner.lower():
        return False, f"`{label}` was applied by @{actor}, not the owner @{owner}"
    if pushed_at is None:
        return False, (f"`{label}` was applied by the owner, but GitHub has no record of when {sha[:7]} "
                       "was pushed, so the label cannot be dated against it")
    applied = parse_time(newest.get("created_at"))
    if applied is None or applied <= pushed_at:
        return False, (f"`{label}` was applied by the owner at {newest.get('created_at')}, before {sha[:7]} "
                       f"was pushed at {_stamp(pushed_at)}; the owner re-applies it after reviewing the new commits")
    reason = (f"`{label}` was applied by the owner @{actor} at {_stamp(applied)}, after {sha[:7]} "
              f"was pushed at {_stamp(pushed_at)}")
    if label != APPROVAL_LABEL or not from_approve_gate(comments, owner, applied):
        return True, reason
    record = recorded_head(comments, label, owner, applied)
    if record is None:
        return False, (f"`{label}` was applied through `/approve-gate`, but no unedited head record from the owner "
                       f"was posted at or after it, so it may cover a head pushed while it was applied; the owner "
                       f"approves {sha[:7]} again")
    seen, posted = record
    if seen != sha:
        return False, (f"`{label}` was applied through `/approve-gate`, but the owner's head record names "
                       f"{seen[:7]}, not the head {sha[:7]}; the owner approves the head after reviewing it")
    return True, (f"{reason}, through `/approve-gate`, and the owner's head record posted at {_stamp(posted)} "
                  f"names {sha[:7]}")


class GitHub:
    """The GitHub API calls the pull_request_target mode makes."""

    def __init__(self, api, token):
        self.api, self.token = api, token

    def get(self, path):
        return api_request(self.api + path, self.token)[0]

    def pages(self, path):
        return api_pages(self.api, path, self.token)


def git_diff(repo_dir, base, head):
    # Full context lets ini/TOML edits be placed in their section. No external
    # diff or textconv driver runs, whatever the repository's attributes say.
    out = subprocess.run(
        ["git", "-C", repo_dir, "-c", "core.quotePath=false", "diff", "--no-color", "--no-ext-diff",
         "--no-textconv", "--find-renames", "--src-prefix=a/", "--dst-prefix=b/", "--unified=1000000",
         base, head],
        check=True, capture_output=True)
    return out.stdout.decode("utf-8", "replace")


def fetch_diff(url, token, base, head):
    """(diff, TreeFacts) from base to head, fetched as objects into an empty repository.

    Nothing is checked out, so no file from the pull request reaches the disk,
    let alone runs; the trees are read as data. The token reaches git through
    the environment, never the command line or a config file.
    """
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    if token:
        n = int(env.get("GIT_CONFIG_COUNT") or 0)
        basic = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")
        env.update({"GIT_CONFIG_COUNT": str(n + 1), f"GIT_CONFIG_KEY_{n}": "http.extraheader",
                    f"GIT_CONFIG_VALUE_{n}": f"AUTHORIZATION: basic {basic}"})
    tmp = tempfile.mkdtemp(prefix="gate-guard-", dir=os.environ.get("RUNNER_TEMP"))
    try:
        subprocess.run(["git", "init", "-q", tmp], check=True, capture_output=True, env=env)
        subprocess.run(["git", "-C", tmp, "fetch", "-q", "--no-tags", "--no-recurse-submodules", "--depth=1",
                        url, base, head], check=True, capture_output=True, env=env)
        return git_diff(tmp, base, head), tree_facts(GitTrees(tmp, base, head))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class Judgment(collections.namedtuple("Judgment", "number base hits error labels verdicts approved findings_blocked")):
    """The verdict on one pull request: its hits against its base, and its owner-only labels."""
    __slots__ = ()

    @property
    def passed(self):
        return not self.error and self.approved and not self.findings_blocked


def render_judgment(j):
    """The report for one pull request: its hits and its owner-only labels."""
    approved, note = j.verdicts[APPROVAL_LABEL]
    title = f"Gate guard: #{j.number} into `{j.base}`"
    if j.error:
        out = [f"### {title}", "", f"**Failing:** {j.error}.", ""]
    else:
        out = [render_summary(j.hits, approved, note, title)]
    if j.findings_blocked:
        out += [f"**Failing:** {j.verdicts[FINDINGS_LABEL][1]}. Only the owner's `{FINDINGS_LABEL}` label, "
                "applied after the newest push, accepts review findings; the owner re-applies or removes it.", ""]
    out += ["#### Owner-only labels", "", "| Label | Counts | Why |", "| --- | --- | --- |"]
    for label in OWNER_LABELS:
        counts, why = j.verdicts[label]
        out.append(f"| `{label}` | {'yes' if counts else 'no'} | {_cell(why)} |")
    return "\n".join(out) + "\n"


def render_target_summary(judgments, sha, guard_sha, list_error=None):
    out = []
    if not judgments:
        out += [f"### Gate guard: head `{sha[:12]}`", "",
                f"**Failing:** {list_error}." if list_error else
                "No open pull request has this head, so there is nothing to judge. The pull request that closed "
                "isn't judged; reopening it, or opening another with this head, judges the head again.", ""]
    elif len(judgments) > 1:
        failing = ", ".join(f"#{j.number}" for j in judgments if not j.passed) or "none"
        out += [f"{len(judgments)} open pull requests have head `{sha[:12]}`. A check belongs to the commit, "
                f"not to one pull request, so `{REQUIRED_CHECK}` passes only when every one of them passes. "
                f"Failing: {failing}.", ""]
    out += [render_judgment(j) for j in judgments]
    guard = f" with `gate_guard.py` at `{guard_sha[:12]}`" if guard_sha else ""
    out += [f"Judged head `{sha[:12]}` against each pull request's current base with the default branch's "
            f"workflow{guard}. The pull requests' files are read only as a diff; none of them run."]
    return "\n".join(out) + "\n"


def _judgment_title(j):
    if j.error:
        return "Could not judge this pull request"
    if j.findings_blocked:
        return f"{FINDINGS_LABEL} does not count"
    if not j.hits:
        return "No removed tests, skips, retries or gate-config edits"
    count = f"{len(j.hits)} hit{'s' if len(j.hits) != 1 else ''}"
    return f"{count}, approved by the owner" if j.approved else f"{count}, not approved"


def check_title(judgments, list_error=None):
    if not judgments:
        if list_error:
            return "Could not list the open pull requests with this head"
        return "No open pull request has this head"
    if len(judgments) == 1:
        return _judgment_title(judgments[0])
    failing = [j for j in judgments if not j.passed]
    if not failing:
        return f"All {len(judgments)} pull requests with this head pass"
    return f"#{failing[0].number} into {failing[0].base}: {_judgment_title(failing[0])}"


def fit_bytes(text, limit=MAX_SUMMARY_BYTES, suffix=TRUNCATED):
    """text, cut so that it and the suffix fit in limit UTF-8 bytes.

    GitHub counts bytes, not characters: a report of non-ASCII paths can be
    far longer in bytes than in characters.
    """
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[:limit - len(suffix.encode("utf-8"))].decode("utf-8", "ignore") + suffix


def print_inert(text):
    """Print text so that no line of it acts as a workflow command.

    The report quotes paths, ref names and error messages; a line of it that
    starts with :: must not add an annotation, a mask or anything else.
    """
    if os.environ.get("GITHUB_ACTIONS") != "true":
        print(text)
        return
    token = secrets.token_hex(16)
    print(f"::stop-commands::{token}")
    print(text)
    print(f"::{token}::")


def verdict_commands(judgments, conclusion, title, error):
    """The workflow commands that carry the verdict on the judging job's own check.

    One notice states it, starting with the hit count, which pr_body_lint.py
    reads. A job that could not judge gives an error instead, so nothing reads
    it as clean.
    """
    if error:
        out = [f"::error title={_escape_property(title)}::{_escape_data(error)}"]
    else:
        hits = sum(len(j.hits) for j in judgments)
        out = [f"::notice title={_escape_property(VERDICT_TITLE)}::"
               f"{_escape_data(f'hits={hits}; {conclusion}; {title}')}"]
    for j in judgments:
        out += [a for a in annotations(j.hits, j.approved) if a not in out]
    return out[:MAX_ANNOTATIONS + 1]


def _write_outputs(values):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.writelines(f"{k}={v}\n" for k, v in values.items())


def _target_hits(github, repo, base_ref, sha, server, token):
    """(hits, error) for what the pull request changes since its merge base."""
    try:
        compare = github.get(f"/repos/{repo}/compare/{urllib.parse.quote(base_ref, safe='/')}...{sha}?per_page=1")
        merge_base = compare["merge_base_commit"]["sha"]
        diff, facts = fetch_diff(f"{server}/{repo}.git", token, merge_base, sha)
        return analyze(parse_diff(diff), facts), None
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or b"").decode("utf-8", "replace").strip()
        return [], f"could not fetch the pull request's commits ({detail or e})"
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as e:
        return [], f"could not read the pull request's diff ({e})"


def _target_labels(github, repo, number, head, sha, owner):
    """(labels on the PR, {label: (counts, reason)}) for the owner-only labels."""
    labels = {l["name"].lower() for l in github.pages(f"/repos/{repo}/issues/{number}/labels")}
    events, comments, pushed_at = [], [], None
    if APPROVAL_LABEL in labels:
        comments = github.pages(f"/repos/{repo}/issues/{number}/comments")
    if labels & set(OWNER_LABELS):
        events = github.pages(f"/repos/{repo}/issues/{number}/events")
        head_repo = (head.get("repo") or {}).get("full_name")
        ref = f"refs/heads/{head.get('ref')}"
        if head_repo:
            activity = github.pages(f"/repos/{head_repo}/activity?ref={urllib.parse.quote(ref, safe='/')}")
            pushed_at = head_pushed_at(activity, ref, sha)
    return labels, {label: owner_label(label, labels, events, owner, sha, pushed_at, comments)
                    for label in OWNER_LABELS}


def judge_pull_request(github, repo, pr, sha, owner, server, token):
    """The Judgment of one pull request whose head is sha, against the base pr names."""
    base, head = pr["base"]["ref"], pr.get("head") or {}
    hits, error = _target_hits(github, repo, base, sha, server, token)
    try:
        labels, verdicts = _target_labels(github, repo, pr["number"], head, sha, owner)
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as e:
        error = error or f"could not read the pull request's labels ({e})"
        labels, verdicts = set(), {label: (False, f"could not read the labels ({e})") for label in OWNER_LABELS}
    approved = not hits or verdicts[APPROVAL_LABEL][0]
    findings_blocked = FINDINGS_LABEL in labels and not verdicts[FINDINGS_LABEL][0]
    return Judgment(pr["number"], base, hits, error, labels, verdicts, approved, findings_blocked)


def pull_requests_with_head(github, repo, sha, event_pr):
    """(pull requests, error): every open pull request whose head is sha, as the API has it now.

    A check run lands on a commit, which any number of pull requests can share,
    each with its own base and labels. The listing gives each one's current
    base, which a queued event's payload may predate. Reconcile each candidate,
    including the event's PR even if unlisted, with a fresh single-PR read.
    Only its current state/head/base decide whether and how it is judged;
    neither an old event nor a stale listing can exclude a reopened PR or
    restore a closed one. An unresolved current state fails closed.
    """
    try:
        numbers = {p["number"] for p in github.pages(f"/repos/{repo}/pulls?state=open")
                   if (p.get("head") or {}).get("sha") == sha}
        error = None
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as e:
        numbers, error = set(), f"could not list the open pull requests with this head ({e})"
    numbers.add(event_pr["number"])
    prs = []
    for number in sorted(numbers):
        try:
            pr = github.get(f"/repos/{repo}/pulls/{number}")
            if (pr["number"] != number or pr["state"] not in {"open", "closed"}
                    or not pr["head"]["sha"] or not pr["base"]["ref"]):
                raise ValueError("incomplete current pull request")
            if pr["state"] == "open" and pr["head"]["sha"] == sha:
                prs.append(pr)
        except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as e:
            error = error or f"could not read pull request #{number}'s current state ({e})"
    return prs, error


def run_target(args, github=None):
    """Judge every open pull request with the event's head; the exit status is the verdict on that head."""
    pr = load_event(os.environ.get("GITHUB_EVENT_PATH")).get("pull_request") or {}
    head, base = pr.get("head") or {}, pr.get("base") or {}
    repo = args.repo or os.environ.get("GITHUB_REPOSITORY")
    sha = head.get("sha")
    if not (repo and pr.get("number") and sha and base.get("ref")):
        print("gate-guard: --pull-request-target needs a pull_request_target event in $GITHUB_EVENT_PATH",
              file=sys.stderr)
        return 2
    owner = args.owner or os.environ.get("GITHUB_REPOSITORY_OWNER") or repo.split("/")[0]
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    github = github or GitHub(os.environ.get("GITHUB_API_URL", "https://api.github.com"), token)
    prs, list_error = pull_requests_with_head(github, repo, sha, pr)
    judgments = [judge_pull_request(github, repo, p, sha, owner, server, token) for p in prs]
    if list_error:
        # Without the whole list, another pull request's failure could go unseen.
        judgments = [j._replace(error=j.error or list_error) for j in judgments]
    error = next((j.error for j in judgments if j.error), list_error)
    passed = all(j.passed for j in judgments) and not error
    # A closed pull request can leave none open with this head: there is no verdict to give.
    conclusion = "neutral" if not judgments and passed else "success" if passed else "failure"
    summary = render_target_summary(judgments, sha, args.guard_sha, list_error)
    print_inert(summary)
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as f:
            f.write(fit_bytes(summary, MAX_SUMMARY_BYTES))
    _write_outputs({"conclusion": conclusion,
                    **{label: str(bool(judgments) and not error and
                                  all(j.verdicts[label][0] for j in judgments)).lower()
                       for label in OWNER_LABELS}})
    if os.environ.get("GITHUB_ACTIONS") == "true":
        for line in verdict_commands(judgments, conclusion, check_title(judgments, list_error), error):
            print(line)
    if error:
        return 2
    return 0 if passed else 1


# ----------------------------------------------------------------------- main

def read_hits(args):
    """The hits of the diff file, or of base to head in the repository, whose trees are read too."""
    if args.diff_file:
        with open(args.diff_file, encoding="utf-8", errors="replace") as f:
            return analyze(parse_diff(f.read()))
    return repo_hits(args.repo_dir, args.base, args.head)


def parse_args(argv):
    p = argparse.ArgumentParser(description="Flag pull request changes that loosen a repository's test gate.")
    p.add_argument("--diff-file", help="read the diff from this file instead of git; the guard then reads only "
                                         "the files the diff shows, not the rest of the trees")
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
    p.add_argument("--pull-request-target", action="store_true",
                   help="judge the pull request of the pull_request_target event in $GITHUB_EVENT_PATH, "
                        "date owner labels against the head push, and exit with the verdict")
    p.add_argument("--guard-sha", help="commit of this script, named in the report")
    return p.parse_args(argv)


def main(argv=None):
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure:
        reconfigure(errors="replace")
    args = parse_args(argv)
    if args.pull_request_target:
        return run_target(args)
    try:
        hits = read_hits(args)
    except (OSError, subprocess.CalledProcessError) as e:
        detail = getattr(e, "stderr", b"") or b""
        print(f"gate-guard: cannot read the diff: {e} {detail.decode('utf-8', 'replace')}".strip(),
              file=sys.stderr)
        return 2
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
