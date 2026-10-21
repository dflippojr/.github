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

    def test_platformio_values_are_judged_on_added_lines(self):
        details = sorted(h.detail for h in self.hits("flag-skip-platformio-values"))
        self.assertEqual(details, ["PlatformIO test_ignore: test_controls"])

    def test_platformio_filter_is_judged_on_removed_entries(self):
        [hit] = self.hits("flag-skip-platformio-filter-narrowed")
        self.assertEqual(hit.detail, "PlatformIO test_filter entry removed: test_controls")
        self.assertEqual(self.hits("pass-platformio-filter-widened"), [])

    def test_platformio_filter_added_to_an_env_is_a_hit(self):
        [hit] = self.hits("flag-skip-platformio-filter-added")
        self.assertEqual(hit.detail, "PlatformIO test_filter added to env:native: runs only test_logic")

    def test_platformio_filter_gained_through_extends_is_a_hit(self):
        [hit] = self.hits("flag-skip-platformio-filter-extends")
        self.assertEqual(hit.detail, "PlatformIO env:native-coverage now inherits a test_filter: runs only test_logic")

    def test_platformio_filter_key_removed_runs_everything(self):
        self.assertEqual(self.hits("pass-platformio-filter-removed"), [])

    def platformio_diff(self, body):
        lines = body.strip("\n").split("\n")
        old = sum(1 for l in lines if l[0] in " -")
        new = sum(1 for l in lines if l[0] in " +")
        header = "diff --git a/platformio.ini b/platformio.ini\n--- a/platformio.ini\n+++ b/platformio.ini\n"
        return header + f"@@ -1,{old} +1,{new} @@\n" + "\n".join(lines) + "\n"

    def test_platformio_filter_added_to_a_default_reaches_each_env_without_one(self):
        for section in ("env", "platformio"):
            with self.subTest(section=section):
                diff = self.platformio_diff(f""" [{section}]
+test_filter = test_logic
 [env:a]
 platform = native
 [env:b]
 platform = native
 test_filter = test_logic test_io
""")
                details = [h.detail for h in analyze_text(diff)]
                self.assertEqual(details, ["PlatformIO env:a now inherits a test_filter: runs only test_logic"])

    def test_platformio_filter_added_to_an_extended_section_reaches_its_children(self):
        diff = self.platformio_diff(""" [base]
 platform = native
+test_filter = test_logic
 [env:a]
 extends = base
 [env:b]
 extends = env:a
""")
        details = sorted(h.detail for h in analyze_text(diff))
        self.assertEqual(details, [
            "PlatformIO env:a now inherits a test_filter: runs only test_logic",
            "PlatformIO env:b now inherits a test_filter: runs only test_logic",
        ])

    def test_platformio_env_with_its_own_filter_ignores_a_new_default(self):
        diff = self.platformio_diff(""" [env]
+test_filter = test_logic
 [env:a]
 test_filter = test_logic test_io
""")
        self.assertEqual(analyze_text(diff), [])

    def test_platformio_new_env_with_a_filter_is_not_a_narrowing(self):
        diff = self.platformio_diff(""" [env:a]
 platform = native
+[env:b]
+extends = env:a
+test_filter = test_logic
""")
        self.assertEqual(analyze_text(diff), [])

    def test_platformio_extends_switch_to_a_narrower_filter_is_a_hit(self):
        diff = self.platformio_diff(""" [env:wide]
 test_filter = test_logic test_io
 [env:narrow]
 test_filter = test_logic
 [env:a]
-extends = env:wide
+extends = env:narrow
""")
        [hit] = analyze_text(diff)
        self.assertEqual(hit.detail, "PlatformIO test_filter entry removed: test_io")

    def test_platformio_extends_cycle_does_not_hang(self):
        diff = self.platformio_diff(""" [env:a]
-extends = env:b
+extends = env:b
 [env:b]
 extends = env:a
""")
        self.assertEqual(analyze_text(diff), [])

    def test_platformio_changed_extends_to_a_section_the_diff_lacks_is_a_hit(self):
        diff = self.platformio_diff(""" [env:wide]
 test_filter = test_logic
 [env:a]
-extends = env:wide
+extends = missing
""")
        [hit] = analyze_text(diff)
        self.assertEqual(hit.detail, "PlatformIO env:a extends a section the diff does not show: missing")

    def test_platformio_later_parent_wins_a_conflicting_filter(self):
        [hit] = self.hits("flag-skip-platformio-filter-extends-order")
        self.assertEqual(hit.detail, "PlatformIO test_filter entry removed: test_controls")
        diff = self.platformio_diff(""" [wide]
 test_filter = test_logic test_controls
 [narrow]
 test_filter = test_logic
 [env:a]
-extends = wide, narrow
+extends = narrow, wide
""")
        self.assertEqual(analyze_text(diff), [])

    def test_platformio_ignore_gained_through_extends_is_a_hit(self):
        [hit] = self.hits("flag-skip-platformio-ignore-extends")
        self.assertEqual(hit.detail, "PlatformIO env:native now inherits test_ignore: test_controls")

    def test_platformio_removed_override_exposes_an_inherited_ignore(self):
        diff = self.platformio_diff(""" [base]
 test_ignore = test_controls
 [env:a]
 extends = base
-test_ignore = test_slow
""")
        [hit] = analyze_text(diff)
        self.assertEqual(hit.detail, "PlatformIO env:a now inherits test_ignore: test_controls")

    def test_platformio_added_ignore_is_reported_once_for_its_children(self):
        diff = self.platformio_diff(""" [env:a]
 platform = native
+test_ignore = test_controls
 [env:b]
 extends = env:a
""")
        [hit] = analyze_text(diff)
        self.assertEqual(hit.detail, "PlatformIO test_ignore: test_ignore = test_controls")

    def test_platformio_ignore_moved_to_a_parent_is_not_a_hit(self):
        diff = self.platformio_diff(""" [base]
 platform = native
+test_ignore = test_controls
 [env:a]
 extends = base
-test_ignore = test_controls
""")
        self.assertEqual(analyze_text(diff), [])

    def test_platformio_ignore_moved_to_a_parent_still_reaches_new_children(self):
        diff = self.platformio_diff(""" [base]
 platform = native
+test_ignore = test_controls
 [env:a]
 extends = base
-test_ignore = test_controls
 [env:b]
 extends = base
""")
        [hit] = analyze_text(diff)
        self.assertEqual(hit.detail, "PlatformIO env:b now inherits test_ignore: test_controls")

    def test_platformio_filter_continuation_stays_with_its_option(self):
        diff = """diff --git a/platformio.ini b/platformio.ini
--- a/platformio.ini
+++ b/platformio.ini
@@ -1,7 +1,6 @@
 [env:native]
 test_ignore =
     test_slow
-    test_old
 test_filter =
     test_logic
 [env:other]
"""
        self.assertEqual(analyze_text(diff), [])

    def test_platformio_empty_override_stops_the_lookup(self):
        [hit] = self.hits("flag-skip-platformio-ignore-empty-override")
        self.assertEqual(hit.detail, "PlatformIO env:native now inherits test_ignore: test_controls")
        diff = self.platformio_diff(""" [filtered]
 test_ignore = test_controls
 [unfiltered]
 test_ignore =
 [env:a]
-extends = filtered, unfiltered
+extends = unfiltered, filtered
""")
        [hit] = analyze_text(diff)
        self.assertEqual(hit.detail, "PlatformIO env:a now inherits test_ignore: test_controls")

    def test_platformio_empty_filter_override_stops_the_lookup(self):
        diff = self.platformio_diff(""" [narrow]
 test_filter = test_logic
 [all]
 test_filter =
 [env:a]
-extends = narrow, all
+extends = all, narrow
""")
        [hit] = analyze_text(diff)
        self.assertEqual(hit.detail, "PlatformIO env:a now inherits a test_filter: runs only test_logic")

    def test_platformio_removed_empty_override_exposes_a_default_through_levels(self):
        for option, detail in [("test_ignore", "PlatformIO env:a now inherits test_ignore: test_controls"),
                               ("test_filter", "PlatformIO env:a now inherits a test_filter: runs only test_controls")]:
            with self.subTest(option=option, where="parent"):
                diff = self.platformio_diff(f""" [env]
 {option} = test_controls
 [base]
-{option} =
+platform = native
 [mid]
 extends = base
 [env:a]
 extends = mid
""")
                self.assertEqual([h.detail for h in analyze_text(diff)], [detail])
            with self.subTest(option=option, where="env"):
                diff = self.platformio_diff(f""" [env]
 {option} = test_controls
 [env:a]
 platform = native
-{option} =
""")
                self.assertEqual([h.detail for h in analyze_text(diff)], [detail])

    def test_platformio_empty_override_unchanged_is_not_a_hit(self):
        diff = self.platformio_diff(""" [env]
 test_ignore = test_controls
 [env:a]
 test_ignore =
-platform = native
+platform = native2
""")
        self.assertEqual(analyze_text(diff), [])

    def test_platformio_interpolated_values_are_resolved(self):
        [hit] = self.hits("flag-skip-platformio-filter-interpolated")
        self.assertEqual(hit.detail, "PlatformIO test_filter entry removed: test_controls")
        [hit] = self.hits("flag-skip-platformio-ignore-interpolated")
        self.assertEqual(hit.detail, "PlatformIO env:native now inherits test_ignore: test_controls")
        self.assertEqual(self.hits("pass-platformio-filter-interpolated-widened"), [])

    def test_platformio_this_reference_follows_the_env(self):
        diff = self.platformio_diff(""" [base]
 suites = test_logic test_controls
 test_filter = ${this.suites}
 [env:a]
 extends = base
+suites = test_logic
""")
        [hit] = analyze_text(diff)
        self.assertEqual(hit.detail, "PlatformIO test_filter entry removed: test_controls")

    def test_platformio_interpolation_through_an_env_and_extends(self):
        diff = self.platformio_diff(""" [narrow]
 suites = test_logic
 [wide]
 suites = test_logic test_controls
 [env:shared]
-extends = wide
+extends = narrow
 [env:a]
 test_ignore = test_soak
 test_filter = ${env:shared.suites}
""")
        [hit] = analyze_text(diff)
        self.assertEqual(hit.detail, "PlatformIO test_filter entry removed: test_controls")

    def test_platformio_unresolved_reference_is_flagged_when_a_dependency_changes(self):
        for option in ("test_filter", "test_ignore"):
            with self.subTest(option=option):
                diff = self.platformio_diff(f""" [common]
-extra = test_logic
+extra = test_io
 [env:a]
 {option} = ${{common.extra}} ${{elsewhere.suites}}
""")
                [hit] = analyze_text(diff)
                self.assertEqual(hit.detail, f"PlatformIO env:a {option} uses a value the guard cannot resolve: "
                                             "${elsewhere.suites}")
                unchanged = self.platformio_diff(f""" [env:a]
-platform = native
+platform = native2
 {option} = ${{common.extra}} ${{elsewhere.suites}}
""")
                self.assertEqual(analyze_text(unchanged), [])

    def test_platformio_option_names_ignore_case(self):
        [hit] = self.hits("flag-skip-platformio-uppercase")
        self.assertEqual(hit.detail, "PlatformIO test_ignore: TEST_IGNORE = test_controls")
        diff = self.platformio_diff(""" [env:a]
 platform = native
+Test_Filter = test_logic
""")
        [hit] = analyze_text(diff)
        self.assertEqual(hit.detail, "PlatformIO test_filter added to env:a: runs only test_logic")
        diff = self.platformio_diff(""" [unfiltered]
 platform = native
 [filtered]
 Test_Ignore = test_controls
 [env:a]
-EXTENDS = unfiltered
+EXTENDS = filtered
""")
        [hit] = analyze_text(diff)
        self.assertEqual(hit.detail, "PlatformIO env:a now inherits test_ignore: test_controls")
        diff = self.platformio_diff(""" [env:a]
 TEST_IGNORE =
     test_slow
+    test_controls
""")
        [hit] = analyze_text(diff)
        self.assertEqual(hit.detail, "PlatformIO test_ignore: test_controls")

    def test_platformio_section_names_keep_their_case(self):
        diff = self.platformio_diff(""" [base]
 platform = native
 [Base]
 test_ignore = test_controls
 [env:a]
-extends = base
+extends = Base
""")
        [hit] = analyze_text(diff)
        self.assertEqual(hit.detail, "PlatformIO env:a now inherits test_ignore: test_controls")

    def test_unity_registration_removal_survives_a_definition_edit(self):
        [hit] = self.hits("flag-fewer-tests-cpp-reformatted")
        self.assertEqual(hit.detail, "1 fewer test in this file; removed test_button_press")

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

    def git(self, *args):
        subprocess.run(["git", "-C", self.root, "-c", "core.autocrlf=false", *args], check=True,
                       capture_output=True)

    def commit(self, files, message):
        for p in pathlib.Path(self.root).iterdir():
            if p.name != ".git":
                shutil.rmtree(p) if p.is_dir() else p.unlink()
        for rel, text in files.items():
            target = pathlib.Path(self.root, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(text.encode("utf-8"))
        self.git("add", "-A")
        self.git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", message)

    def test_fixtures_through_git(self):
        for name in ["flag-deleted-test-files", "flag-gate-coverage-python", "flag-skip-js",
                     "pass-rename-test-file", "pass-split-test-file", "flag-deleted-test-cpp",
                     "flag-fewer-tests-cpp", "flag-skip-platformio", "pass-rename-test-file-cpp"]:
            with self.subTest(fixture=name), tempfile.TemporaryDirectory() as tmp:
                self.root = tmp
                self.git("init", "-q")
                text = (FIXTURES / f"{name}.diff").read_text(encoding="utf-8")
                before, after = _trees(text)
                self.commit(before, "base")
                self.commit(after, "head")
                code, out = run_main(["--repo-dir", tmp, "--base", "HEAD^1", "--head", "HEAD"])
                expected = gate_guard.render_summary(analyze_text(text), False,
                                                     "no pull request to read labels from")
                self.assertEqual(out.strip(), expected.strip())
                self.assertEqual(code, 1 if name.startswith("flag-") else 0)


if __name__ == "__main__":
    unittest.main()
