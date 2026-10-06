import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import sync_labels  # noqa: E402

SLUG = f"{sync_labels.OWNER}/r"


def label(name, color="aaaaaa", description="d"):
    return {"name": name, "color": color, "description": description}


def run_plan(monkeypatch, existing, config):
    have = {l["name"].lower(): l for l in existing}
    monkeypatch.setattr(sync_labels, "current_labels", lambda repo: have)
    return sync_labels.plan("r", config)


def config(labels, renames=None, remove=None):
    return {"labels": labels, "renames": renames or {}, "remove": remove or {}}


def test_missing_label_is_created(monkeypatch):
    steps = run_plan(monkeypatch, [], config([label("new", "111111", "desc")]))
    assert steps == [("create new", ["label", "create", "new", "-R", SLUG,
                                     "--color", "111111", "--description", "desc"])]


def test_changed_color_or_description_is_edited(monkeypatch):
    steps = run_plan(monkeypatch, [label("a", "aaaaaa", "old")],
                     config([label("a", "bbbbbb", "new")]))
    assert len(steps) == 1
    desc, args = steps[0]
    assert "color aaaaaa -> bbbbbb" in desc and "description" in desc
    assert args == ["label", "edit", "a", "-R", SLUG, "--color", "bbbbbb", "--description", "new"]


def test_unchanged_label_has_no_steps(monkeypatch):
    assert run_plan(monkeypatch, [label("a")], config([label("a")])) == []


def test_rename_when_only_old_name_exists(monkeypatch):
    steps = run_plan(monkeypatch, [label("old")], config([label("new")], renames={"old": "new"}))
    assert steps == [("rename old -> new", ["label", "edit", "old", "-R", SLUG, "--name", "new"])]


def test_both_old_and_new_exist_warns_without_rename(monkeypatch, capsys):
    steps = run_plan(monkeypatch, [label("old"), label("new")],
                     config([label("new")], renames={"old": "new"}))
    assert steps == []
    out = capsys.readouterr().out
    assert "both old and new exist" in out


def test_case_only_name_difference_renames_via_edit(monkeypatch):
    steps = run_plan(monkeypatch, [label("p0")], config([label("P0")]))
    assert len(steps) == 1
    desc, args = steps[0]
    assert "name 'p0'" in desc
    assert args[:3] == ["label", "edit", "p0"] and args[-2:] == ["--name", "P0"]
    assert not any(a == "create" for a in args)


def test_remove_entry_deletes_only_when_present(monkeypatch):
    cfg = config([], remove={"r": ["gone"], "other": ["x"]})
    steps = run_plan(monkeypatch, [label("gone"), label("keep")], cfg)
    assert steps == [("delete gone", ["label", "delete", "gone", "-R", SLUG, "--yes"])]
    assert run_plan(monkeypatch, [label("keep")], cfg) == []
