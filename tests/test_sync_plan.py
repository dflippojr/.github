import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import sync_labels  # noqa: E402

SLUG = f"{sync_labels.OWNER}/r"
REAL_CONFIG = json.loads((ROOT / "labels.json").read_text(encoding="utf-8"))


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


USER_ACTION = label("user-action", "d93f0b", "Needs the owner to do something (purchase, credential, decision)")
USER_PRESENT = label("user-present", "f9d0c4", "The owner or a physical device is needed for the next step")


def test_real_config_never_renames_user_action(monkeypatch):
    steps = run_plan(monkeypatch, [USER_ACTION], REAL_CONFIG)
    assert not [desc for desc, _ in steps if desc.startswith("rename")]


def test_real_config_user_action_beside_user_present_has_no_warning(monkeypatch, capsys):
    run_plan(monkeypatch, [USER_ACTION, USER_PRESENT], REAL_CONFIG)
    assert "both" not in capsys.readouterr().out
