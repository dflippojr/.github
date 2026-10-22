import io
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import gate_guard  # noqa: E402

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "gate_guard"


def fixture_expectations(path):
    """(rules, hit count) from a fixture's '# expect:' and '# hits:' header lines."""
    text = path.read_text(encoding="utf-8")
    rules = re.search(r"^# expect:(.*)$", text, re.M).group(1).split()
    hits = int(re.search(r"^# hits: (\d+)$", text, re.M).group(1))
    return set(rules), hits


def analyze_text(text):
    return gate_guard.analyze(gate_guard.parse_diff(text))


class Link(str):
    """A symbolic link's target, as a value of git_commit's files."""


def git_commit(root, files, message):
    """Replace the work tree of the repository at root with files (path -> text, bytes or Link) and commit it."""
    def git(*args, **kwargs):
        return subprocess.run(["git", "-C", root, "-c", "core.autocrlf=false", *args], check=True,
                              capture_output=True, **kwargs).stdout
    for p in pathlib.Path(root).iterdir():
        if p.name != ".git":
            shutil.rmtree(p) if p.is_dir() else p.unlink()
    for rel, text in files.items():
        if isinstance(text, Link):
            continue
        target = pathlib.Path(root, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
    git("add", "-A")
    # Links go straight into the index, so the test needs no symbolic link support from the file system.
    for rel, text in files.items():
        if isinstance(text, Link):
            sha = git("hash-object", "-w", "--stdin", input=text.encode("utf-8")).decode("ascii").strip()
            git("update-index", "--add", "--cacheinfo", f"120000,{sha},{rel}")
    git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", message)


def judge_commits(base, head):
    """The hits for a base and a head commit of a scratch repository, read through git as the workflows read it."""
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["git", "init", "-q", tmp], check=True, capture_output=True)
        git_commit(tmp, base, "base")
        git_commit(tmp, head, "head")
        return gate_guard.repo_hits(tmp, "HEAD^1", "HEAD")


def clean_env(extra=None):
    """The process environment without GitHub Actions variables, plus extra."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GITHUB_", "GH_"))}
    env.update(extra or {})
    return env


def run_main(argv, env=None):
    out = io.StringIO()
    with mock.patch.dict(os.environ, clean_env(env), clear=True), mock.patch("sys.stdout", out):
        code = gate_guard.main(argv)
    return code, out.getvalue()


class Fixtures(unittest.TestCase):
    def test_every_rule_has_fixtures(self):
        names = [p.stem for p in FIXTURES.glob("*.diff")]
        flagged = set()
        for p in FIXTURES.glob("flag-*.diff"):
            flagged |= fixture_expectations(p)[0]
        self.assertEqual(flagged, {"removed-tests", "skip-marker", "retry", "gate-config"})
        self.assertTrue(any(n.startswith("pass-") for n in names))

    def test_fixture_verdicts(self):
        for path in sorted(FIXTURES.glob("*.diff")):
            with self.subTest(fixture=path.name):
                rules, count = fixture_expectations(path)
                hits = analyze_text(path.read_text(encoding="utf-8"))
                self.assertEqual({h.rule for h in hits}, rules, hits)
                self.assertEqual(len(hits), count, hits)
                # The file name says which way the fixture should go.
                self.assertEqual(bool(hits), path.name.startswith("flag-"))

    def test_cli_exit_codes(self):
        for path in sorted(FIXTURES.glob("*.diff")):
            with self.subTest(fixture=path.name):
                code, _ = run_main(["--diff-file", str(path)])
                self.assertEqual(code, 1 if path.name.startswith("flag-") else 0)


class RuleDetails(unittest.TestCase):
    def hits(self, name):
        return analyze_text((FIXTURES / f"{name}.diff").read_text(encoding="utf-8"))

    def test_deleted_test_module_names_its_tests(self):
        details = {h.path: h.detail for h in self.hits("flag-deleted-test-files")}
        self.assertIn("3 tests removed: test_add, test_sub, test_zero", details["tests/test_calc.py"])
        self.assertEqual(details["tests/test_placeholder.py"], "test file deleted")

    def test_rename_in_place_is_not_a_removal(self):
        [hit] = self.hits("flag-fewer-tests-python")
        self.assertIn("1 fewer test", hit.detail)
        self.assertIn("removed test_sub", hit.detail)
        self.assertIn("added test_zero_plus_zero", hit.detail)

    def test_marker_languages_name_the_removed_test(self):
        self.assertIn("removesItem", self.hits("flag-fewer-tests-java")[0].detail)
        self.assertIn("overflows", self.hits("flag-fewer-tests-rust")[0].detail)

    def test_unity_run_test_removal_names_the_test(self):
        [hit] = self.hits("flag-fewer-tests-cpp")
        self.assertEqual(hit.path, "firmware/test/test_controls/test_main.cpp")
        self.assertEqual(hit.detail, "1 fewer test in this file; removed test_button_press")

    def test_deleted_unity_test_file_names_its_tests(self):
        [hit] = self.hits("flag-deleted-test-cpp")
        self.assertEqual(hit.detail, "test file deleted, 2 tests removed: test_clamp_low, test_clamp_high")

    def test_unity_test_and_registration_count_once(self):
        diff = """diff --git a/test/test_a/test_main.cpp b/test/test_a/test_main.cpp
--- a/test/test_a/test_main.cpp
+++ b/test/test_a/test_main.cpp
@@ -1,7 +1,2 @@
-static void test_a(void) {}
-void test_b() {}
-void test_c(int);
 int main() {
-  RUN_TEST(test_a);
-  RUN_TEST(test_b);
 }
"""
        [hit] = analyze_text(diff)
        self.assertEqual(hit.detail, "2 fewer tests in this file; removed test_a, test_b")

    def test_c_sources_outside_a_test_directory_are_not_tests(self):
        diff = """diff --git a/src/main.cpp b/src/main.cpp
--- a/src/main.cpp
+++ b/src/main.cpp
@@ -1,2 +1,1 @@
-void test_probe(void) {}
 int x;
"""
        self.assertEqual(analyze_text(diff), [])

    def platformio_diff(self, body, path="platformio.ini"):
        lines = body.strip("\n").split("\n")
        old = sum(1 for l in lines if l[0] in " -")
        new = sum(1 for l in lines if l[0] in " +")
        header = f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
        return header + f"@@ -1,{old} +1,{new} @@\n" + "\n".join(lines) + "\n"

    def assert_platformio_hit(self, body, path="platformio.ini"):
        hits = analyze_text(self.platformio_diff(body, path))
        self.assertTrue(hits and all(h.rule == "skip-marker" for h in hits), hits)
        return hits

    def test_platformio_test_option_change_names_it(self):
        [hit] = self.hits("flag-skip-platformio")
        self.assertEqual(hit.detail, "PlatformIO [env:native] test_ignore added: test_controls")
        [hit] = self.hits("flag-skip-platformio-filter-narrowed")
        self.assertEqual(hit.detail, "PlatformIO [env:native] test_filter changed: test_logic test_controls -> test_logic")
        [hit] = self.hits("flag-skip-platformio-filter-removed")
        self.assertEqual(hit.detail, "PlatformIO [env:native] test_filter removed, was: test_logic test_controls")
        [hit] = self.hits("flag-skip-platformio-ignore-extends")
        self.assertEqual(hit.detail, "PlatformIO [env:native] extends changed: unfiltered -> filtered")

    def test_platformio_any_test_or_extends_change_is_a_hit(self):
        # Each was a probe of the resolver that read inheritance; the guard no
        # longer resolves it, so each change to a test_* or extends option is a hit,
        # whether PlatformIO would run fewer tests (the first group) or not (the second).
        narrowing = [
            """ [env]
+test_filter = test_logic
 [env:a]
 platform = native
""", """ [base]
 platform = native
+test_filter = test_logic
 [env:a]
 extends = base
""", """ [env:wide]
 test_filter = test_logic test_io
 [env:narrow]
 test_filter = test_logic
 [env:a]
-extends = env:wide
+extends = env:narrow
""", """ [wide]
 test_filter = test_logic, test_controls
 [narrow]
 test_filter = test_logic
 [env:a]
-extends = narrow, wide
+extends = wide, narrow
""", """ [base]
 test_ignore = test_controls
 [env:a]
 extends = base
-test_ignore = test_slow
""", """ [env:a]
 platform = native
+test_ignore = test_controls
 [env:b]
 extends = env:a
""", """ [filtered]
 test_ignore = test_controls
 [unfiltered]
 test_ignore =
 [env:a]
-extends = filtered, unfiltered
+extends = unfiltered, filtered
""", """ [env]
 test_filter = test_controls
 [env:a]
 platform = native
-test_filter =
""", """ [env:a]
 platform = native
+Test_Filter = test_logic
""", """ [unfiltered]
 platform = native
 [filtered]
 Test_Ignore = test_controls
 [env:a]
-EXTENDS = unfiltered
+EXTENDS = filtered
""", """ [env:a]
 TEST_IGNORE =
     test_slow
+    test_controls
""", """ [base]
 platform = native
 [Base]
 test_ignore = test_controls
 [env:a]
-extends = base
+extends = Base
""", """ [DEFAULT]
+test_filter = test_logic
 [env:a]
 platform = native
""", """ [env:a]
 platform = native
-[env:b]
 test_ignore = test_controls
""", """ [env:a]
 test_filter =
     test_logic
+build_flags = -DFAST
     test_controls
"""]
        not_narrowing = [
            """ [platformio]
+test_filter = test_logic
 [env:a]
 platform = native
""", """ [env]
+test_filter = test_logic
 [env:a]
 test_filter = test_logic test_io
""", """ [env:a]
 platform = native
+[env:b]
+extends = env:a
+test_filter = test_logic
""", """ [base]
 platform = native
+test_ignore = test_controls
 [env:a]
 extends = base
-test_ignore = test_controls
""", """ [env:native]
 test_ignore =
     test_slow
-    test_old
 test_filter =
     test_logic
""", """ [common]
-test_filter = test_logic
+test_filter =
 [env:a]
 extends = common
""", """ [env:a]
-test_framework = unity
+test_framework = googletest
""", """ [platformio]
-test_dir = test
+test_dir = tests
""", """ [env:a]
+test_build_src = yes
"""]
        for body in narrowing + not_narrowing:
            with self.subTest(body=body):
                self.assert_platformio_hit(body)

    def test_platformio_references_fail_closed_on_any_change(self):
        for name in ("flag-skip-platformio-filter-interpolated", "flag-skip-platformio-ignore-defaulted",
                     "flag-skip-platformio-filter-interpolated-widened"):
            with self.subTest(fixture=name):
                [hit] = self.hits(name)
                self.assertTrue(hit.detail.endswith("reads other values, which the guard does not resolve, "
                                                    "and this change edits the file"), hit.detail)
        # Whatever the reference reads, and whether or not it changed.
        for value in ("${common.extra}", "${this.suites}", "${env:shared.suites}", "${sysenv.TEST_SUITES}",
                      "${PROJECT_DIR}", "$PROJECT_HASH", "%(suites)s", "${platformio.build_dir}/test_controls"):
            with self.subTest(value=value):
                self.assert_platformio_hit(f""" [common]
-extra = test_logic
+extra = test_io
 [env:a]
 test_filter = {value}
""")
        # A reference in extends counts too.
        self.assert_platformio_hit(""" [narrow]
 test_filter = test_logic
 [env:a]
-platform = native
+platform = native2
 extends = ${common.base}
""")
        # A comment is no change to the file.
        self.assertEqual(analyze_text(self.platformio_diff(""" [env:native]
 test_ignore = *${platformio.build_dir}/test_controls
+; a comment
""")), [])

    def test_platformio_change_that_does_not_touch_tests_is_not_a_hit(self):
        for body in (""" [env]
 test_ignore = test_controls
 [env:a]
 test_ignore =
-platform = native
+platform = native2
""", """ [env:a]
 test_ignore =
     test_controls
+; a comment
 build_flags = -DFAST
"""):
            with self.subTest(body=body):
                self.assertEqual(analyze_text(self.platformio_diff(body)), [])
        # A line of a test option that ConfigParser reads the same is still touched.
        for body in (""" [env:a]
-test_filter = test_logic
 platform = native
+test_filter = test_logic
""", """ [env:a]
-test_ignore = test_controls ; flaky
+test_ignore = test_controls  # flaky on CI
""", """ [env:a]
 test_ignore =
     test_controls
+    ; test_soak
"""):
            with self.subTest(body=body):
                [hit] = self.assert_platformio_hit(body)
                self.assertTrue(hit.detail.startswith("PlatformIO platformio.ini edits a test_* or extends option line: "),
                                hit.detail)

    def test_platformio_extra_configs_fail_closed(self):
        detail = ("PlatformIO firmware/platformio.ini loads extra_configs the guard cannot read, "
                  "so it cannot tell what this change runs")
        for name in ("flag-skip-platformio-extra-configs", "flag-skip-platformio-extra-configs-inherited"):
            with self.subTest(fixture=name):
                [hit] = self.hits(name)
                self.assertEqual(hit.detail, detail)
        # [DEFAULT] reaches [platformio], and a section [platformio] may extend sets it too.
        for section in ("DEFAULT", "includes"):
            with self.subTest(section=section):
                diff = self.platformio_diff(f""" [{section}]
 extra_configs = test.ini
 [selection]
-suites = test_logic, test_controls
+suites = test_logic
 [env:native]
 platform = native
""")
                [hit] = analyze_text(diff)
                self.assertEqual(hit.detail, detail.replace("firmware/", ""))

    def test_platformio_included_file_change_fails_closed(self):
        loaded = ("PlatformIO test.ini may be loaded through extra_configs by a platformio.ini the diff does not "
                  "show, so the guard cannot tell what this change runs")
        [hit] = self.hits("flag-skip-platformio-extra-config-default")
        self.assertEqual(hit.detail, "PlatformIO [env] test_filter added: test_logic")
        [hit] = self.hits("flag-skip-platformio-extra-config-env")
        self.assertEqual(hit.detail, "PlatformIO [env:native] test_filter added: test_logic")
        for body in (""" [DEFAULT]
+test_filter = test_logic
 [env]
 test_framework = unity
""", """ [env:native]
-extends = unfiltered
+extends = filtered
"""):
            with self.subTest(body=body):
                self.assert_platformio_hit(body, "test.ini")
        [hit] = self.assert_platformio_hit(""" [suites]
-list = test_logic, test_controls
+list = test_logic
 [env]
 test_framework = unity
""", "test.ini")
        self.assertEqual(hit.detail, loaded)
        # Comments, and ini files of other tools, are not PlatformIO changes.
        self.assertEqual(analyze_text(self.platformio_diff(""" [env]
+; a comment
 test_framework = unity
""", "test.ini")), [])
        self.assertEqual(analyze_text(self.platformio_diff(""" [tox]
-envlist = py311, py312
+envlist = py312
""", "tox.ini")), [])

    def test_platformio_file_the_guard_cannot_read_is_flagged(self):
        [hit] = self.assert_platformio_hit(""" [env:a]
 test_filter = test_logic, test_controls
+test_filter = test_logic
""")
        self.assertTrue(hit.detail.startswith("PlatformIO platformio.ini cannot be read: "), hit.detail)
        diff = """diff --git a/platformio.ini b/platformio.ini
--- a/platformio.ini
+++ b/platformio.ini
@@ -10,2 +10,2 @@
-    test_controls
+    test_soak
 build_flags = -DFAST
"""
        [hit] = analyze_text(diff)
        self.assertEqual(hit.detail, "PlatformIO platformio.ini is not shown whole in the diff, so the guard cannot read it")

    def test_unity_registration_removal_survives_a_definition_edit(self):
        [hit] = self.hits("flag-fewer-tests-cpp-reformatted")
        self.assertEqual(hit.detail, "1 fewer test in this file; removed test_button_press")

    def test_unity_unregistered_definition_does_not_offset_a_removed_registration(self):
        [hit] = self.hits("flag-fewer-tests-cpp-unregistered")
        self.assertEqual(hit.detail, "1 fewer test in this file; removed test_button_press; "
                                     "added test_button_release")

    def test_unity_registrations_are_read_across_lines_and_comments(self):
        [hit] = self.hits("flag-fewer-tests-cpp-multiline")
        self.assertEqual(hit.detail, "1 fewer test in this file; removed test_button_press")
        main = "test/test_a/test_main.cpp"
        for body in (""" void test_a(void) {}
 void test_b(void) {}
 int main() {
-  RUN_TEST(test_a); RUN_TEST(test_b);
+  RUN_TEST(test_a);
 }
""", """ void test_a(void) {}
 void test_b(void) {}
 int main() {
   RUN_TEST(test_a);
+  /*
   RUN_TEST(test_b);
+  */
 }
""", """ void test_a(void) {}
 void test_b(void) {}
 int main() {
   RUN_TEST(test_a);
-  RUN_TEST(test_b);
+  puts("RUN_TEST(test_b);");
 }
""", """ void test_a(void) {}
 void test_b(void) {}
 int main() {
   RUN_TEST(test_a);
-  RUN_TEST(test_b);
+  // RUN_TEST(test_b);
 }
""", """ void test_a(void) {}
 void test_b(void) {}
 int main() {
   RUN_TEST(test_a);
-  RUN_TE\\
-ST(test_b);
 }
"""):
            with self.subTest(body=body):
                [hit] = analyze_text(self.platformio_diff(body, main))
                self.assertEqual(hit.detail, "1 fewer test in this file; removed test_b")
        # A registration reflowed across lines, or a definition split from its name, is the same test.
        self.assertEqual(analyze_text(self.platformio_diff(""" void test_a(void) {}
-void test_b(void) {}
+void
+test_b(void) {}
 int main() {
-  RUN_TEST(test_a); RUN_TEST(test_b);
+  RUN_TEST(test_a);
+  RUN_TEST(
+      test_b);
 }
""", main)), [])

    def test_unity_file_the_diff_does_not_show_whole_fails_closed(self):
        diff = """diff --git a/test/test_a/test_main.cpp b/test/test_a/test_main.cpp
--- a/test/test_a/test_main.cpp
+++ b/test/test_a/test_main.cpp
@@ -10,2 +10,2 @@
-  RUN_TEST(test_a);
+  RUN_TEST(test_a );
   RUN_TEST(test_b);
"""
        [hit] = analyze_text(diff)
        self.assertEqual(hit.detail, "Unity test file is not shown whole in the diff, so the guard cannot count its tests")

    def test_unity_preprocessor_and_other_frameworks_fail_closed(self):
        main = "test/test_a/test_main.cpp"
        unmodelled = {
            "a macro that calls RUN_TEST": """ #define RUN(name) RUN_TEST(name)
 void test_a(void) {}
 void test_b(void) {}
 int main() {
   RUN(test_a);
-  RUN(test_b);
 }
""",
            "a GoogleTest test is removed": """ #include <gtest/gtest.h>
 TEST(Controls, Press) {}
-TEST(Controls, Release) {}
""",
            "a RUN_TEST the guard cannot read is removed": """ void test_a(void) {}
 int main() {
-  RUN_TEST((test_a));
 }
""",
        }
        for name, body in unmodelled.items():
            with self.subTest(name):
                hits = analyze_text(self.platformio_diff(body, main))
                self.assertTrue(any(h.rule == "removed-tests" and h.detail.startswith("Unity test file ")
                                    for h in hits), hits)

    def test_unity_preprocessor_change_fails_closed(self):
        main = "test/test_a/test_main.cpp"
        detail = ("Unity test file adds, removes, moves or edits a preprocessor directive, "
                  "which can turn tests off and which the guard does not evaluate")
        for name in ("flag-fewer-tests-cpp-elif-changed", "flag-fewer-tests-cpp-undef-moved",
                     "flag-fewer-tests-cpp-preprocessor-edit"):
            with self.subTest(fixture=name):
                [hit] = self.hits(name)
                self.assertEqual(hit.detail, detail)
        changes = {
            "a registration wrapped in #if 0": """ void test_a(void) {}
 int main() {
+#if 0
   RUN_TEST(test_a);
+#endif
 }
""",
            "a changed condition": """ void test_a(void) {}
 int main() {
-#ifdef ARDUINO
+#ifdef NEVER_DEFINED
   RUN_TEST(test_a);
 #endif
 }
""",
            "an inserted earlier #elif": """ void test_a(void) {}
 int main() {
 #if 0
+#elif 1
 #elif FAST
 #else
   RUN_TEST(test_a);
 #endif
 }
""",
            "a changed condition around a #define": """ #if 1
-#define ENABLE_A 1
+#define ENABLE_A 0
 #endif
 void test_a(void) {}
 int main() {
 #if ENABLE_A
   RUN_TEST(test_a);
 #endif
 }
""",
            "a #define added": """ void test_a(void) {}
+#define test_a test_b
 int main() {
   RUN_TEST(test_a);
 }
""",
            "an #include removed": """-#include "more_tests.inc"
 void test_a(void) {}
 int main() {
   RUN_TEST(test_a);
 }
""",
            "an #include added": """ #include <unity.h>
+#include "extra.h"
 void test_a(void) {}
 int main() {
   RUN_TEST(test_a);
 }
""",
            "an edited continuation line": """ #define ENABLE_A \\
-    1
+    0
 void test_a(void) {}
 int main() {
 #if ENABLE_A
   RUN_TEST(test_a);
 #endif
 }
""",
            "a line spliced onto a directive": """ void test_a(void) {}
 int main() {
+  int unused; \\
 #if 0
   RUN_TEST(test_a);
 #endif
 }
""",
            "a comment edited on a directive line": """-#include <unity.h>
+#include <unity.h> // the test runner
 void test_a(void) {}
 int main() {
   RUN_TEST(test_a);
 }
""",
            "new tests in a new #ifdef block": """ #include <unity.h>
 void test_a(void) {}
+#ifdef ARDUINO
+void test_b(void) {}
+#endif
 int main() {
   RUN_TEST(test_a);
+#ifdef ARDUINO
+  RUN_TEST(test_b);
+#endif
 }
""",
        }
        for name, body in changes.items():
            with self.subTest(name):
                hits = analyze_text(self.platformio_diff(body, main))
                self.assertIn(detail, [h.detail for h in hits])
        # A header in a test directory can turn tests off too.
        header = "test/test_a/config.h"
        for body in ("""-#define ENABLE_A 1
+#define ENABLE_A 0
""", """-#define ENABLE_A 1
"""):
            with self.subTest(header=body):
                [hit] = analyze_text(self.platformio_diff(body, header))
                self.assertEqual(hit.detail, detail.replace("Unity test file", "C/C++ header in a test directory"))
        deleted = f"diff --git a/{header} b/{header}\ndeleted file mode 100644\n--- a/{header}\n+++ /dev/null\n" \
                  "@@ -1 +0,0 @@\n-#define ENABLE_A 1\n"
        [hit] = analyze_text(deleted)
        self.assertEqual(hit.path, header)

    def test_unity_move_across_an_unchanged_directive_is_a_removal(self):
        main = "test/test_a/test_main.cpp"
        for body in (""" void test_a(void) {}
 void test_b(void) {}
 int main() {
-  RUN_TEST(test_a);
 #if 0
   RUN_TEST(test_b);
+  RUN_TEST(test_a);
 #endif
 }
""", """ void test_a(void) {}
 int main() {
 #if 0
+  RUN_TEST(test_a);
 #else
-  RUN_TEST(test_a);
 #endif
 }
"""):
            with self.subTest(body=body):
                hits = analyze_text(self.platformio_diff(body, main))
                self.assertIn("1 fewer test in this file; removed test_a; added test_a", [h.detail for h in hits])
        # Reordering registrations between the same directives is not a removal.
        self.assertEqual(analyze_text(self.platformio_diff(""" #include <unity.h>
 void test_a(void) {}
 void test_b(void) {}
 int main() {
-  RUN_TEST(test_a);
   RUN_TEST(test_b);
+  RUN_TEST(test_a);
 }
""", main)), [])

    def test_unity_move_into_a_branch_of_another_file_is_a_removal(self):
        removed = self.platformio_diff(""" void test_a(void) {}
-void test_b(void) {}
 int main() {
   RUN_TEST(test_a);
-  RUN_TEST(test_b);
 }
""", "test/test_a/test_main.cpp")
        diff = removed + self.platformio_diff(""" void test_c(void) {}
+#if 0
+void test_b(void) {}
+#endif
 int main() {
   RUN_TEST(test_c);
+#if 0
+  RUN_TEST(test_b);
+#endif
 }
""", "test/test_c/test_main.cpp")
        hits = {h.path: h.detail for h in analyze_text(diff)}
        self.assertEqual(hits["test/test_a/test_main.cpp"], "1 fewer test in this file; removed test_b")
        self.assertIn("preprocessor directive", hits["test/test_c/test_main.cpp"])
        # A file with a #define may rename the test, so a move into one is not paired either.
        diff = removed + self.platformio_diff(""" #define test_b test_c
 void test_c(void) {}
+void test_b(void) {}
 int main() {
   RUN_TEST(test_c);
+  RUN_TEST(test_b);
 }
""", "test/test_c/test_main.cpp")
        [hit] = analyze_text(diff)
        self.assertEqual(hit.path, "test/test_a/test_main.cpp")

    def test_unity_alternative_directive_spellings_fail_closed(self):
        main = "test/test_a/test_main.cpp"
        detail = ("Unity test file adds, removes, moves or edits a preprocessor directive, "
                  "which can turn tests off and which the guard does not evaluate")
        [hit] = self.hits("flag-fewer-tests-cpp-digraph-directive")
        self.assertEqual(hit.detail, detail)
        wrapped = """ void test_a(void) {{}}
 int main() {{
+{0}
   RUN_TEST(test_a);
+{1}
 }}
"""
        for opening, closing in (("%:if 0", "%:endif"), ("??=if 0", "??=endif"), ("  %:  if 0", "%: endif"),
                                 ("/* off */ %:if 0", "%:endif"), ("/* off\n+*/ %:if 0", "%:endif"),
                                 ("\f#if 0", "\v#endif"), ("#", "# 7 \"x.c\"")):
            with self.subTest(opening=opening):
                hits = analyze_text(self.platformio_diff(wrapped.format(opening, closing), main))
                self.assertTrue(any(detail in h.detail for h in hits), hits)
        # A trigraph can splice a registration into a comment.
        [hit] = analyze_text(self.platformio_diff(""" void test_a(void) {}
 int main() {
+  // the next line is commented out ??/
   RUN_TEST(test_a);
 }
""", main))
        self.assertEqual(hit.detail, "Unity test file uses a trigraph, which the guard does not translate")
        # An edit next to an unchanged digraph directive is still fine.
        self.assertEqual(analyze_text(self.platformio_diff(""" %:include <unity.h>
 void test_a(void) {}
+void test_b(void) {}
 int main() {
   RUN_TEST(test_a);
+  RUN_TEST(test_b);
 }
""", main)), [])

    def test_unity_registrations_in_test_headers_are_counted(self):
        [hit] = self.hits("flag-fewer-tests-cpp-header")
        self.assertEqual((hit.path, hit.detail), ("firmware/test/test_a/cases.h",
                                                  "1 fewer test in this file; removed test_controls"))
        for ext in (".hh", ".hpp", ".hxx", ".inc", ".ipp"):
            with self.subTest(ext=ext):
                [hit] = analyze_text(self.platformio_diff(""" void register_cases(void) {
     RUN_TEST(test_logic);
-    RUN_TEST(test_controls);
 }
""", f"test/test_a/cases{ext}"))
                self.assertEqual(hit.detail, "1 fewer test in this file; removed test_controls")
        # A header's tests run only where a source includes it, so a registration
        # moved from a source into a header does not pair; neither does one moved out.
        diff = self.platformio_diff(""" #include "cases.h"
 void test_logic(void) {}
 int main() {
-  RUN_TEST(test_logic);
+  register_cases();
 }
""", "test/test_a/test_main.cpp") + self.platformio_diff(""" void register_cases(void) {
+  RUN_TEST(test_logic);
 }
""", "test/test_a/cases.h")
        [hit] = analyze_text(diff)
        self.assertEqual((hit.path, hit.detail), ("test/test_a/test_main.cpp",
                                                  "1 fewer test in this file; removed test_logic"))

    def test_other_files_in_a_c_test_directory_fail_closed(self):
        detail = ("changes a file in a C/C++ test directory that is not a C/C++ source or header; a test source "
                  "may #include it, so the guard cannot count its tests")
        [hit] = self.hits("flag-fewer-tests-cpp-included-fragment")
        self.assertEqual((hit.rule, hit.path, hit.detail), ("removed-tests", "firmware/test/test_a/cases.def", detail))
        source = self.platformio_diff(""" #include <unity.h>
 int main() {
 }
""", "test/test_a/test_main.cpp")
        for path in ("test/test_a/cases.txt", "test/shared/CASES", "test/test_a/data.json"):
            with self.subTest(path=path):
                [hit] = analyze_text(source + self.platformio_diff("-RUN_TEST(test_b);\n", path))
                self.assertEqual((hit.path, hit.detail), (path, detail))
        # Outside a test directory that holds C/C++ files, the rule does not apply.
        self.assertEqual(analyze_text(self.platformio_diff("-a\n+b\n", "tests/fixtures/data.json")), [])
        # A symbolic link's target is not read, whatever its name.
        link = ("diff --git a/test/test_a/cases.h b/test/test_a/cases.h\nindex 1b2c3d4..5e6f7a8 120000\n"
                "--- a/test/test_a/cases.h\n+++ b/test/test_a/cases.h\n@@ -1 +1 @@\n-../../shared/full.h\n"
                "\\ No newline at end of file\n+../../shared/min.h\n\\ No newline at end of file\n")
        [hit] = analyze_text(source + link)
        self.assertEqual((hit.path, hit.detail), ("test/test_a/cases.h", (
            "changes a symbolic link or submodule in a C/C++ test directory, which the guard does not follow, "
            "so it cannot count the tests it holds")))

    def test_unity_tests_in_a_platformio_test_dir_are_counted(self):
        source = "void test_a(void) {}\nvoid test_b(void) {}\nint main() {\n  RUN_TEST(test_a);\n  RUN_TEST(test_b);\n}\n"
        fewer = source.replace("  RUN_TEST(test_b);\n", "")
        for config in ("[platformio]\ntest_dir = unit\n", "[env]\nbuild_flags = -DX\n[platformio]\ntest_dir = ./checks/unit\n"):
            root = config.split("= ")[-1].strip().lstrip("./")
            with self.subTest(config=config):
                base = {"fw/platformio.ini": config, f"fw/{root}/test_a/main.cpp": source}
                [hit] = judge_commits(base, dict(base, **{f"fw/{root}/test_a/main.cpp": fewer}))
                self.assertEqual(hit.detail, "1 fewer test in this file; removed test_b")
                [hit] = judge_commits(base, {"fw/platformio.ini": config})
                self.assertTrue(hit.detail.startswith("test file deleted"), hit)
                [hit] = judge_commits(base, dict(base, **{f"fw/{root}/test_a/cases.txt": "RUN_TEST(test_c);\n"}))
                self.assertEqual(hit.path, f"fw/{root}/test_a/cases.txt")
        # Without test_dir, unit/ is not a test directory.
        base = {"fw/platformio.ini": "[env:native]\n", "fw/unit/test_a/main.cpp": source}
        self.assertEqual(judge_commits(base, dict(base, **{"fw/unit/test_a/main.cpp": fewer})), [])
        base = {"fw/platformio.ini": "[platformio]\ntest_dir = ${PROJECT_DIR}/unit\n"}
        [hit] = judge_commits(dict(base, **{"src/a.c": "int a;\n"}), dict(base, **{"src/a.c": "int b;\n"}))
        self.assertIn("test_dir entry ${PROJECT_DIR}/unit reads other values and cannot be resolved", hit.detail)

    def test_unity_has_include_fails_closed_on_added_and_removed_files(self):
        base = {"test/test_a/test_main.cpp": "#if __has_include(\"skip.h\")\n#else\nRUN_TEST(test_a);\n#endif\n"}
        hits = judge_commits(base, dict(base, **{"include/skip.h": ""}))
        detail = ("C/C++ test source test/test_a/test_main.cpp tests whether files exist with __has_include, "
                  "and this change adds or removes a file")
        self.assertEqual([(h.path, h.detail) for h in hits], [("include/skip.h", detail)])
        self.assertEqual(judge_commits(dict(base, **{"src/a.c": "int a;\n"}), dict(base, **{"src/a.c": "int b;\n"})),
                         [])

    def test_platformio_files_loaded_by_an_unchanged_config_fail_closed(self):
        [hit] = self.hits("flag-skip-platformio-extra-config-custom")
        self.assertEqual(hit.path, "firmware/selection.ini")
        self.assertEqual(hit.detail, "PlatformIO firmware/platformio.ini loads firmware/selection.ini through "
                                     "extra_configs, and this change edits it")
        [hit] = self.hits("flag-skip-platformio-extra-config-conf")
        self.assertEqual((hit.path, hit.detail),
                         ("firmware/test.conf", "PlatformIO [env:native] test_ignore added: test_controls"))
        main = "[platformio]\nextra_configs =\n  configs/*.ini\n  extra/**/*.cfg, nested.ini\n[env:native]\n"
        base = {"platformio.ini": main, "configs/a.ini": "[selection]\nsuites = a, b\n",
                "extra/deep/x.cfg": "[x]\ny = 1\n", "nested.ini": "[platformio]\nextra_configs = more.txt\n",
                "more.txt": "[x]\ny = 1\n", "other/tox.ini": "[tox]\nenvlist = py311\n"}
        for changed, text in (("configs/a.ini", "[selection]\nsuites = a\n"), ("configs/b.ini", "[x]\n"),
                              ("extra/deep/x.cfg", "[x]\ny = 2\n"), ("more.txt", "[x]\ny = 2\n")):
            with self.subTest(changed=changed):
                [hit] = judge_commits(base, dict(base, **{changed: text}))
                self.assertEqual((hit.rule, hit.path), ("skip-marker", changed))
                self.assertIn("through extra_configs", hit.detail)
        deleted = dict(base)
        del deleted["configs/a.ini"]
        [hit] = judge_commits(base, deleted)
        self.assertEqual(hit.path, "configs/a.ini")
        # A file nothing loads is not a PlatformIO change.
        self.assertEqual(judge_commits(base, dict(base, **{"other/tox.ini": "[tox]\nenvlist = py312\n"})), [])

    def test_platformio_discovery_that_cannot_complete_fails_closed(self):
        change = ({"src/a.c": "int a;\n"}, {"src/a.c": "int b;\n"})
        for value, why in (("${sysenv.CFG}.ini", "cannot be resolved"), ("~/pio.ini", "cannot be resolved"),
                           ("/etc/pio.ini", "cannot be resolved"), ("../../x.ini", "cannot be resolved"),
                           ("%(here)s/x.ini", "cannot be resolved")):
            with self.subTest(value=value):
                config = {"fw/platformio.ini": f"[platformio]\nextra_configs = {value}\n"}
                [hit] = judge_commits(dict(config, **change[0]), dict(config, **change[1]))
                self.assertEqual((hit.rule, hit.path), ("skip-marker", "fw/platformio.ini"))
                self.assertIn(why, hit.detail)
        # PlatformIO's glob follows a symbolic link the guard cannot; so does a submodule's path.
        config = {"platformio.ini": "[platformio]\nextra_configs = shared/*.ini\n", "shared": Link("../common")}
        [hit] = judge_commits(dict(config, **change[0]), dict(config, **change[1]))
        self.assertEqual(hit.path, "platformio.ini")
        self.assertIn("passes through a symbolic link or submodule", hit.detail)
        for config in ({"platformio.ini": "[env:a]\ntest_filter = a\ntest_filter = b\n"},
                       {"platformio.ini": "[platformio]\nextra_configs = x.ini\n", "x.ini": Link("y.ini")},
                       {"platformio.ini": "[platformio]\nextra_configs = x.ini\n", "x.ini": "no section\n"},
                       {"platformio.ini": "[platformio]\nextra_configs = x.ini\n", "x.ini": b"[a]\nb = \xff\n"}):
            with self.subTest(config=config):
                [hit] = judge_commits(dict(config, **change[0]), dict(config, **change[1]))
                self.assertEqual(hit.rule, "skip-marker")
                self.assertIn("cannot be read, so the guard cannot tell which files PlatformIO loads", hit.detail)

    def test_environment_guard_skip_is_not_a_hit(self):
        details = " ".join(h.detail for h in self.hits("flag-skip-python"))
        self.assertIn("flaky", details)
        self.assertIn("slow on CI", details)
        self.assertNotIn("node isn't installed", details)

    def test_retry_details_show_the_change(self):
        details = sorted(h.detail for h in self.hits("flag-retry-playwright"))
        self.assertTrue(details[0].startswith("retries raised from 1 to 3"), details)
        self.assertTrue(details[1].startswith("retries set to 2"), details)

    def test_lint_hits(self):
        details = sorted(h.detail for h in self.hits("flag-gate-lint"))
        self.assertEqual(details, [
            'lint rule disabled: "E501",',
            'lint rule disabled: "no-console": "off",',
            "lint rule disabled: W503,",
            'lint setting removed: "B",',
        ])

    def test_budget_only_flags_the_raised_key(self):
        details = [h.detail for h in self.hits("flag-gate-budget")]
        self.assertEqual(sum("maximumWarning" in d for d in details), 1)


class Parser(unittest.TestCase):
    def test_removed_lines_that_look_like_headers(self):
        diff = ("diff --git a/q.sql b/q.sql\n--- a/q.sql\n+++ b/q.sql\n@@ -1,3 +1,2 @@\n"
                " select 1;\n--- a comment\n-+++ odd\n+select 2;\n")
        [fd] = gate_guard.parse_diff(diff)
        self.assertEqual([(l.kind, l.text) for l in fd.lines],
                         [(" ", "select 1;"), ("-", "-- a comment"), ("-", "+++ odd"), ("+", "select 2;")])

    def test_crlf_no_newline_marker_and_preamble(self):
        diff = ("preamble text\r\ndiff --git a/t.py b/t.py\r\n--- a/t.py\r\n+++ b/t.py\r\n@@ -1 +1 @@\r\n"
                "-def test_a():\r\n\\ No newline at end of file\r\n+def test_b():\r\n")
        [fd] = gate_guard.parse_diff(diff)
        self.assertEqual([(l.kind, l.old, l.new, l.text) for l in fd.lines],
                         [("-", 1, None, "def test_a():"), ("+", None, 1, "def test_b():")])

    def test_quoted_paths_and_binary_deletion(self):
        diff = ('diff --git "a/tests/t\\303\\251st_x.py" "b/tests/t\\303\\251st_x.py"\n'
                "deleted file mode 100644\nBinary files \"a/tests/t\\303\\251st_x.py\" and /dev/null differ\n"
                "diff --git a/my dir/a b.py b/my dir/a b.py\nnew file mode 100644\n")
        deleted, added = gate_guard.parse_diff(diff)
        self.assertEqual((deleted.status, deleted.path), ("deleted", "tests/tést_x.py"))
        self.assertEqual((added.status, added.path), ("added", "my dir/a b.py"))

    def test_rename_headers(self):
        diff = ("diff --git a/tests/test_a.py b/tests/unit/test_a.py\nsimilarity index 100%\n"
                "rename from tests/test_a.py\nrename to tests/unit/test_a.py\n")
        [fd] = gate_guard.parse_diff(diff)
        self.assertEqual((fd.status, fd.old_path, fd.new_path), ("renamed", "tests/test_a.py", "tests/unit/test_a.py"))
        self.assertEqual(analyze_text(diff), [])


class Classification(unittest.TestCase):
    def test_test_files(self):
        for path in ["tests/test_a.py", "pkg/a_test.py", "conftest.py", "tests/helpers.py", "web/cart.spec.ts",
                     "src/__tests__/cart.js", "calc/calc_test.go", "src/test/java/CartTest.java", "e2e/app.ts"]:
            self.assertTrue(gate_guard.is_test_file(path), path)
        for path in ["src/app.py", "src/cart.ts", "calc/calc.go", "src/main/java/Cart.java", "README.md"]:
            self.assertFalse(gate_guard.is_test_file(path), path)

    def test_c_and_cpp_test_modules(self):
        for path in ["firmware/test/test_controls/test_main.cpp", "test/test_a/main.c", "tests/unit/a.cc",
                     "lib/x/test/b.cxx"]:
            self.assertTrue(gate_guard.is_test_module(path), path)
        for path in ["firmware/src/main.cpp", "firmware/test/test_controls/helpers.h", "src/test.cpp",
                     "firmware/platformio.ini"]:
            self.assertFalse(gate_guard.is_test_module(path), path)

    def test_config_files(self):
        for path in ["pytest.ini", "package.json", "playwright.config.ts", ".eslintrc.js", "requirements-dev.txt",
                     "pom.xml", "build.gradle.kts", "Makefile"]:
            self.assertTrue(gate_guard.is_config(path), path)
        for path in ["src/app.py", "src/cart.ts", "README.md", "docs/notes.txt"]:
            self.assertFalse(gate_guard.is_config(path), path)


class Approval(unittest.TestCase):
    def fetch(self, labels, events=()):
        responses = {"/repos/o/r/issues/7/labels": [{"name": n} for n in labels],
                     "/repos/o/r/issues/7/events": list(events)}
        return lambda path: responses[path]

    @staticmethod
    def labeled(actor, event="labeled", label=gate_guard.APPROVAL_LABEL):
        return {"event": event, "label": {"name": label}, "actor": {"login": actor}}

    def test_label_missing(self):
        approved, note = gate_guard.label_approval("o/r", 7, "owner", False, self.fetch(["ready"]))
        self.assertFalse(approved)
        self.assertIn("not on this pull request", note)

    def test_label_present(self):
        approved, _ = gate_guard.label_approval("o/r", 7, "owner", False, self.fetch(["Gate-Change-Approved"]))
        self.assertTrue(approved)

    def test_owner_applied(self):
        events = [self.labeled("worker", label="ready"), self.labeled("Owner")]
        approved, note = gate_guard.label_approval("o/r", 7, "owner", True,
                                                   self.fetch([gate_guard.APPROVAL_LABEL], events))
        self.assertTrue(approved)
        self.assertIn("@Owner", note)

    def test_last_application_counts(self):
        events = [self.labeled("owner"), self.labeled("owner", "unlabeled"), self.labeled("worker-bot")]
        approved, note = gate_guard.label_approval("o/r", 7, "owner", True,
                                                   self.fetch([gate_guard.APPROVAL_LABEL], events))
        self.assertFalse(approved)
        self.assertIn("applied by @worker-bot, not the owner @owner", note)

    def test_no_pull_request_context(self):
        args = gate_guard.parse_args([])
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(gate_guard.approval(args), (False, "no pull request to read labels from"))

    def test_api_failure_fails_closed(self):
        args = gate_guard.parse_args(["--repo", "o/r", "--pr", "7"])
        error = gate_guard.urllib.error.URLError("offline")
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(gate_guard.urllib.request, "urlopen", side_effect=error):
            approved, note = gate_guard.approval(args)
        self.assertFalse(approved)
        self.assertIn("could not read", note)

    def test_pr_number_from_event_and_owner_from_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            event = pathlib.Path(tmp) / "event.json"
            event.write_text(json.dumps({"pull_request": {"number": 7}}), encoding="utf-8")
            env = {"GITHUB_REPOSITORY": "o/r", "GITHUB_EVENT_PATH": str(event), "GITHUB_REPOSITORY_OWNER": "o",
                   "GITHUB_TOKEN": "t"}
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(gate_guard, "label_approval", return_value=(True, "ok")) as la:
                gate_guard.approval(gate_guard.parse_args(["--require-owner-label-actor"]))
        self.assertEqual(la.call_args[0][:4], ("o/r", 7, "o", True))

    def test_api_pages_follows_next_links(self):
        class Response(io.BytesIO):
            def __init__(self, items, link):
                super().__init__(json.dumps(items).encode())
                self.headers = {"Link": link}

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.close()

        pages = [Response([1, 2], '<https://api.test/next?page=2>; rel="next", <https://api.test/last>; rel="last"'),
                 Response([3], "")]
        with mock.patch.object(gate_guard.urllib.request, "urlopen", side_effect=pages) as urlopen:
            items = gate_guard.api_pages("https://api.test", "/repos/o/r/issues/7/labels", "tok")
        self.assertEqual(items, [1, 2, 3])
        first, second = (c.args[0] for c in urlopen.call_args_list)
        self.assertEqual(first.full_url, "https://api.test/repos/o/r/issues/7/labels?per_page=100")
        self.assertEqual(first.get_header("Authorization"), "Bearer tok")
        self.assertEqual(second.full_url, "https://api.test/next?page=2")

    def test_api_pages_fails_closed_on_a_cut_off_list(self):
        """Label events come oldest first; the pages past the limit hold the newest ones."""
        def request(url, _token):
            return [{"event": "labeled"}], f'<{url}&more>; rel="next"'

        with mock.patch.object(gate_guard, "api_request", side_effect=request) as api_request:
            with self.assertRaisesRegex(gate_guard.IncompleteList, "more than 30 pages"):
                gate_guard.api_pages("https://api.test", "/repos/o/r/issues/7/events", "tok")
        self.assertEqual(api_request.call_count, gate_guard.MAX_API_PAGES)

    def test_cut_off_label_history_fails_closed(self):
        args = gate_guard.parse_args(["--repo", "o/r", "--pr", "7", "--require-owner-label-actor"])
        cut_off = gate_guard.IncompleteList("too long")
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(gate_guard, "api_pages", side_effect=cut_off):
            self.assertEqual(gate_guard.approval(args), (False, "could not read the pull request's labels (too long)"))


class Main(unittest.TestCase):
    def test_approved_hits_pass_with_warnings_and_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            summary = pathlib.Path(tmp) / "summary.md"
            env = {"GITHUB_ACTIONS": "true", "GITHUB_STEP_SUMMARY": str(summary)}
            with mock.patch.object(gate_guard, "approval", return_value=(True, "the label is on it")):
                code, out = run_main(["--diff-file", str(FIXTURES / "flag-skip-rust.diff")], env)
            text = summary.read_text(encoding="utf-8")
        self.assertEqual(code, 0)
        self.assertIn("**Passing with 1 hit:** the label is on it.", text)
        self.assertIn("| skip-marker | `src/lib.rs:10` | Rust #[ignore]: #[ignore] |", text)
        self.assertIn("::warning file=src/lib.rs,line=10,title=gate-guard skip-marker::", out)

    def test_unapproved_hits_fail_with_errors(self):
        env = {"GITHUB_ACTIONS": "true"}
        with mock.patch.object(gate_guard, "approval", return_value=(False, "no label")):
            code, out = run_main(["--diff-file", str(FIXTURES / "flag-gate-workflow-sonar.diff")], env)
        self.assertEqual(code, 1)
        self.assertIn("**Failing with 2 hits:** no label.", out)
        self.assertIn("::error file=.github/workflows/ci.yml,title=gate-guard gate-config::workflow modified", out)

    def test_clean_diff_skips_the_label_lookup(self):
        with mock.patch.object(gate_guard, "approval") as approval:
            code, out = run_main(["--diff-file", str(FIXTURES / "pass-tightening.diff")])
        self.assertEqual(code, 0)
        approval.assert_not_called()
        self.assertIn("No removed tests, skip markers, retries or gate-config edits.", out)

    def test_unreadable_diff_exits_2(self):
        with mock.patch("sys.stderr", io.StringIO()):
            code, _ = run_main(["--diff-file", "no/such/file.diff"])
        self.assertEqual(code, 2)

    def test_annotation_escaping(self):
        hit = gate_guard.Hit("retry", "a,b:c.py", 3, "50% more\nretries")
        [line] = gate_guard.annotations([hit], approved=False)
        self.assertEqual(line, "::error file=a%2Cb%3Ac.py,line=3,title=gate-guard retry::50%25 more%0Aretries")

    def test_summary_escapes_pipes(self):
        text = gate_guard.render_summary([gate_guard.Hit("retry", "a.ts", 1, "x || y")], False, "no label")
        self.assertIn("| retry | `a.ts:1` | x \\|\\| y |", text)


def _trees(diff_text):
    """Before and after file trees rebuilt from a full-context fixture diff."""
    before, after = {}, {}
    for fd in gate_guard.parse_diff(diff_text):
        if fd.old_path:
            before[fd.old_path] = "".join(l.text + "\n" for l in fd.lines if l.kind != "+")
        if fd.new_path:
            after[fd.new_path] = "".join(l.text + "\n" for l in fd.lines if l.kind != "-")
    return before, after


class GitEndToEnd(unittest.TestCase):
    """The CLI diffs two real commits the way the workflow does and reaches the same verdict."""

    def test_fixtures_through_git(self):
        for name in ["flag-deleted-test-files", "flag-gate-coverage-python", "flag-skip-js",
                     "pass-rename-test-file", "pass-split-test-file", "flag-deleted-test-cpp",
                     "flag-fewer-tests-cpp", "flag-skip-platformio", "pass-rename-test-file-cpp",
                     "flag-fewer-tests-cpp-digraph-directive", "flag-skip-platformio-extra-config-custom",
                     "flag-skip-platformio-extra-config-conf", "flag-fewer-tests-cpp-header",
                     "flag-fewer-tests-cpp-included-fragment"]:
            with self.subTest(fixture=name), tempfile.TemporaryDirectory() as tmp:
                subprocess.run(["git", "init", "-q", tmp], check=True, capture_output=True)
                text = (FIXTURES / f"{name}.diff").read_text(encoding="utf-8")
                before, after = _trees(text)
                git_commit(tmp, before, "base")
                git_commit(tmp, after, "head")
                code, out = run_main(["--repo-dir", tmp, "--base", "HEAD^1", "--head", "HEAD"])
                expected = gate_guard.render_summary(analyze_text(text), False,
                                                     "no pull request to read labels from")
                self.assertEqual(out.strip(), expected.strip())
                self.assertEqual(code, 1 if name.startswith("flag-") else 0)


if __name__ == "__main__":
    unittest.main()
