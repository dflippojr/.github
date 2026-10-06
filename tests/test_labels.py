import collections
import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "labels.json").read_text(encoding="utf-8"))
LABELS = CONFIG["labels"]


def duplicates(pairs):
    seen = collections.defaultdict(list)
    for key, name in pairs:
        seen[key].append(name)
    return {k: v for k, v in seen.items() if len(v) > 1}


def contributing_labels():
    """Label names in the first column of the label table in CONTRIBUTING.md."""
    names = set()
    for line in (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8").splitlines():
        if line.startswith("|"):
            first = line.split("|")[1]
            names.update(re.findall(r"`([^`]+)`", first))
    return names


def test_names_unique_case_insensitive():
    dupes = duplicates((l["name"].lower(), l["name"]) for l in LABELS)
    assert not dupes, f"duplicate label names: {dupes}"


def test_colors_are_lowercase_six_digit_hex():
    bad = [(l["name"], l["color"]) for l in LABELS if not re.fullmatch(r"[0-9a-f]{6}", l["color"])]
    assert not bad, f"colors must be 6-digit lowercase hex: {bad}"


def test_colors_pairwise_distinct():
    dupes = duplicates((l["color"], l["name"]) for l in LABELS)
    assert not dupes, f"labels sharing a color: {dupes}"


def test_descriptions_at_most_100_characters():
    long = [(l["name"], len(l["description"])) for l in LABELS if len(l["description"]) > 100]
    assert not long, f"descriptions over 100 characters: {long}"


def test_rename_targets_exist():
    names = {l["name"] for l in LABELS}
    missing = {old: new for old, new in CONFIG["renames"].items() if new not in names}
    assert not missing, f"rename targets not in labels: {missing}"


def test_contributing_table_matches_labels_json():
    documented = contributing_labels()
    defined = {l["name"] for l in LABELS}
    assert documented == defined, (
        f"in CONTRIBUTING.md only: {sorted(documented - defined)}; "
        f"in labels.json only: {sorted(defined - documented)}")
