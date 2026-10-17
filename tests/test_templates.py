import pathlib
import re

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
FORMS = ROOT / ".github" / "ISSUE_TEMPLATE"
PR_TEMPLATE = (ROOT / "PULL_REQUEST_TEMPLATE.md").read_text(encoding="utf-8")

# The parts of GitHub's issue-form schema these forms use:
# https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests/syntax-for-githubs-form-schema
TOP_LEVEL = {"name", "description", "body", "assignees", "labels", "title", "projects", "type"}
FIELD_KEYS = {
    "markdown": {"attributes"},
    "textarea": {"id", "attributes", "validations"},
    "input": {"id", "attributes", "validations"},
    "dropdown": {"id", "attributes", "validations"},
    "checkboxes": {"id", "attributes", "validations"},
}
ATTRIBUTES = {
    "markdown": {"value"},
    "textarea": {"label", "description", "placeholder", "value", "render"},
    "input": {"label", "description", "placeholder", "value"},
    "dropdown": {"label", "description", "multiple", "options", "default"},
    "checkboxes": {"label", "description", "options"},
}


def load(name):
    return yaml.safe_load((FORMS / name).read_text(encoding="utf-8"))


def fields(form):
    return {f["id"]: f for f in form["body"] if "id" in f}


@pytest.mark.parametrize("name", sorted(p.name for p in FORMS.glob("*.yml") if p.name != "config.yml"))
def test_issue_form_matches_schema(name):
    form = load(name)
    assert set(form) <= TOP_LEVEL, f"unknown top-level keys: {set(form) - TOP_LEVEL}"
    for key in ("name", "description", "body"):
        assert form.get(key), f"missing {key}"
    ids, labels = [], []
    for field in form["body"]:
        kind = field.get("type")
        assert kind in FIELD_KEYS, f"unknown field type {kind!r}"
        assert set(field) - {"type"} <= FIELD_KEYS[kind], f"unknown keys in {field}"
        attrs = field["attributes"]
        assert set(attrs) <= ATTRIBUTES[kind], f"unknown attributes in {field}"
        if kind == "markdown":
            assert attrs.get("value")
            continue
        assert re.fullmatch(r"[A-Za-z0-9_-]+", field.get("id", "")), f"bad id in {field}"
        assert attrs.get("label"), f"missing label in {field}"
        ids.append(field["id"])
        labels.append(attrs["label"])
        if kind == "dropdown":
            options = attrs.get("options")
            assert options and len(set(options)) == len(options), f"dropdown options must be unique: {field}"
        if "validations" in field:
            assert set(field["validations"]) <= {"required"}
            assert isinstance(field["validations"]["required"], bool)
    assert len(set(ids)) == len(ids), f"duplicate ids: {ids}"
    assert len(set(labels)) == len(labels), f"duplicate labels: {labels}"
    assert ids, "a form needs at least one non-markdown field"


def required(field):
    return field.get("validations", {}).get("required") is True


def test_bug_form_requires_introduced_by_and_gate():
    bug = fields(load("bug.yml"))
    assert required(bug["introduced-by"])
    assert required(bug["gate"])
    assert bug["gate"]["attributes"]["options"] == ["tests", "review", "lint", "outside judge", "none existed"]


def test_feature_form_requires_acceptance_criteria():
    criteria = fields(load("feature.yml"))["acceptance-criteria"]
    assert required(criteria)
    # A prefilled value would satisfy "required" untouched, so the example lives in the placeholder.
    assert "value" not in criteria["attributes"]
    assert "- [ ]" in criteria["attributes"]["placeholder"]


def headings():
    return re.findall(r"^#{2,3} .+$", PR_TEMPLATE, flags=re.MULTILINE)


def test_pr_template_sections_in_order():
    assert headings() == [
        "## What changed",
        "## Proof",
        "### Not verified",
        "### Gate changes",
        "## After merge",
    ]


def test_pr_template_proof_table_columns():
    proof = PR_TEMPLATE.split("## Proof", 1)[1].split("### Not verified", 1)[0]
    header = next(line for line in proof.splitlines() if line.startswith("|"))
    columns = [c.strip() for c in header.strip("|").split("|")]
    assert [c.split(" (")[0] for c in columns] == ["Acceptance criterion", "How verified", "Evidence"]
