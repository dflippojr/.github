"""Sync the labels in labels.json to the owner's active repositories.

Active means not archived, not a fork, and pushed within the last 365 days
(or since the --since date). Repository names given on the command line are
checked against the owner's repositories before anything is planned; an
unknown name exits with status 2. Prints the planned changes by default; pass --apply to make them. Labels a repository
has that labels.json does not list are kept unless labels.json names them
under "remove".

    python scripts/sync_labels.py                 # dry run, all active repos
    python scripts/sync_labels.py --apply
    python scripts/sync_labels.py --since 2026-01-01
    python scripts/sync_labels.py --apply plex-webhook agent-loop
"""
import argparse
import datetime
import json
import pathlib
import subprocess
import sys

OWNER = "dflippojr"
CONFIG = pathlib.Path(__file__).resolve().parent.parent / "labels.json"


def gh(*args):
    out = subprocess.run(["gh", *args], check=True, capture_output=True, text=True, encoding="utf-8")
    return out.stdout


def list_repos():
    return json.loads(gh("repo", "list", OWNER, "--limit", "500", "--json", "name,isArchived,isFork,pushedAt"))


def active_repos(repos, today=None, since=None):
    """Names of repos that are not archived, not forks and pushed on or after since.

    since defaults to 365 days before today. pushedAt is an ISO timestamp, so its
    date part compares as a string.
    """
    if since is None:
        since = (today or datetime.date.today()) - datetime.timedelta(days=365)
    cutoff = since.isoformat()
    return sorted(r["name"] for r in repos
                  if not r["isArchived"] and not r["isFork"] and r["pushedAt"][:10] >= cutoff)


def unknown_repos(names, repos):
    known = {r["name"].lower() for r in repos}
    return [n for n in names if n.lower() not in known]


def current_labels(repo):
    labels = json.loads(gh("label", "list", "-R", f"{OWNER}/{repo}", "--limit", "500",
                           "--json", "name,color,description"))
    return {l["name"].lower(): l for l in labels}


def plan(repo, config):
    """Return (description, gh args) steps that bring repo in line with config."""
    have = current_labels(repo)
    slug = f"{OWNER}/{repo}"
    steps = []

    for old, new in config["renames"].items():
        if old.lower() in have and new.lower() not in have:
            steps.append((f"rename {old} -> {new}", ["label", "edit", old, "-R", slug, "--name", new]))
            have[new.lower()] = dict(have.pop(old.lower()), name=new)
        elif old.lower() in have:
            print(f"  ! {repo}: both {old} and {new} exist; move issues by hand, then delete {old}")

    for want in config["labels"]:
        cur = have.get(want["name"].lower())
        args = ["--color", want["color"], "--description", want["description"]]
        if cur is None:
            steps.append((f"create {want['name']}", ["label", "create", want["name"], "-R", slug, *args]))
            continue
        changes = []
        if cur["name"] != want["name"]:
            changes.append(f"name {cur['name']!r}")
            args += ["--name", want["name"]]
        if cur["color"].lower() != want["color"]:
            changes.append(f"color {cur['color'].lower()} -> {want['color']}")
        if cur["description"] != want["description"]:
            changes.append(f"description {cur['description']!r} -> {want['description']!r}")
        if changes:
            steps.append((f"update {want['name']}: " + "; ".join(changes),
                          ["label", "edit", cur["name"], "-R", slug, *args]))

    for name in config["remove"].get(repo, []):
        if name.lower() in have:
            steps.append((f"delete {name}", ["label", "delete", name, "-R", slug, "--yes"]))
    return steps


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("repos", nargs="*", help="repository names (default: all active repos)")
    parser.add_argument("--apply", action="store_true", help="make the changes instead of printing them")
    parser.add_argument("--since", type=datetime.date.fromisoformat, metavar="YYYY-MM-DD",
                        help="count repos pushed on or after this date as active (default: 365 days ago)")
    opts = parser.parse_args()
    config = json.loads(CONFIG.read_text(encoding="utf-8"))

    repos = list_repos()
    missing = unknown_repos(opts.repos, repos)
    if missing:
        print(f"error: unknown repository name(s) for {OWNER}: {', '.join(missing)}", file=sys.stderr)
        return 2

    total = 0
    for repo in opts.repos or active_repos(repos, since=opts.since):
        steps = plan(repo, config)
        total += len(steps)
        print(f"{repo}: {len(steps)} change(s)")
        for desc, args in steps:
            print(f"  {desc}")
            if opts.apply:
                gh(*args)
    print(f"{'Applied' if opts.apply else 'Planned'} {total} change(s).")


if __name__ == "__main__":
    sys.exit(main())
