import datetime
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import sync_labels  # noqa: E402


def repo(name, pushed, archived=False, fork=False):
    return {"name": name, "isArchived": archived, "isFork": fork, "pushedAt": pushed}


REPOS = [
    repo("recent", "2026-12-30T00:00:00Z"),
    repo("edge", "2026-01-02T00:00:00Z"),
    repo("stale", "2026-01-01T23:59:59Z"),
    repo("archived", "2026-12-30T00:00:00Z", archived=True),
    repo("forked", "2026-12-30T00:00:00Z", fork=True),
]


class ActiveRepos(unittest.TestCase):
    def test_new_year_keeps_last_week_pushes(self):
        names = sync_labels.active_repos(REPOS, today=datetime.date(2027, 1, 2))
        self.assertIn("recent", names)

    def test_window_boundary(self):
        # 2027-01-02 minus 365 days is 2026-01-02: pushed that day is in, the day before is out.
        names = sync_labels.active_repos(REPOS, today=datetime.date(2027, 1, 2))
        self.assertEqual(names, ["edge", "recent"])

    def test_excludes_archived_and_forks(self):
        names = sync_labels.active_repos(REPOS, today=datetime.date(2027, 1, 2))
        self.assertNotIn("archived", names)
        self.assertNotIn("forked", names)

    def test_since_override(self):
        names = sync_labels.active_repos(REPOS, since=datetime.date(2026, 1, 1))
        self.assertEqual(names, ["edge", "recent", "stale"])


class UnknownRepos(unittest.TestCase):
    def test_reports_unknown_case_insensitively(self):
        self.assertEqual(sync_labels.unknown_repos(["Recent", "typo-repo"], REPOS), ["typo-repo"])

    def test_main_exits_2_before_any_change(self):
        with mock.patch.object(sync_labels, "list_repos", return_value=REPOS), \
                mock.patch.object(sync_labels, "plan") as plan, \
                mock.patch.object(sync_labels, "gh") as gh, \
                mock.patch.object(sys, "argv", ["sync_labels.py", "--apply", "recent", "typo-repo"]), \
                mock.patch.object(sys, "stderr"):
            self.assertEqual(sync_labels.main(), 2)
        plan.assert_not_called()
        gh.assert_not_called()


if __name__ == "__main__":
    unittest.main()
