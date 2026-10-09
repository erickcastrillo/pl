"""Decide whether a pull request is safe to merge without a person: small, reviewed, low risk.

Run by .github/workflows/auto-merge.yml from the base branch, never from the pull request, so a
pull request cannot change the rules that judge it. It reads the pull request through the GitHub
API only and never runs the pull request's code.

Usage: auto_merge_check.py OWNER/REPO NUMBER EVENT_ACTION
Prints eligible=true|false, then one reason per line when not eligible.
Settings from env: AUTO_MERGE_LABEL (the review label to require), AUTO_MERGE_AUTHORS (comma list).
"""
import fnmatch
import json
from datetime import datetime
import os
import re
import subprocess
import sys

MAX_LINES = 150
MAX_FILES = 6
BASE_BRANCH = "main"

# Changes here always need a person: CI and these rules, dependencies, setup and credentials,
# the assistant's permission rules, the updater, agent-facing instructions and the review skill,
# and any database migration.
SENSITIVE = (".github/*", "pyproject.toml", "uv.lock", "src/pl/setup.py", "src/pl/assistant.py",
             "src/pl/assistant.md", "src/pl/update.py", "src/pl/accounts.py", "src/pl/skills_builtin/*",
             "AGENTS.md", "INSTALL.md", "CLAUDE.md", "*migrations/*", "*.sql")

RISKY = re.compile(r"shell\s*=\s*True|\beval\(|\bexec\(|\bos\.system\b|\bsubprocess\b|\bpickle\b|\byaml\.load\b"
                   r"|\bchmod\b|\burllib\b|\brequests\b|\bsocket\b|__import__"
                   r"|\b(api[_-]?key|password|secret|token|credentials?)\b", re.IGNORECASE)


def _gh(args):
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout


def _when(stamp):
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def labeled_at(pages, label):
    """Return when the review label was last added, from pages of issue events, or None."""
    times = [ev["created_at"] for page in pages for ev in page
             if ev.get("event") == "labeled" and (ev.get("label") or {}).get("name") == label]
    return max(times, key=_when) if times else None


def check(pr, files, action, label, authors, labeled_at=None, committed_at=None):
    """Return the reasons this pull request may not auto-merge; an empty list means it may."""
    why = []
    # A label that does not postdate the last commit reviewed older code. This also covers
    # a run for new commits that was cancelled before it removed the label.
    if not labeled_at or not committed_at:
        why.append("there is no record of when the review label was added or the last commit was made")
    elif _when(labeled_at) <= _when(committed_at):
        why.append("the review label was added before the last commit; it needs a fresh review")
    if action == "synchronize":
        why.append("new commits arrived after the review; it needs a fresh review label")
    if pr.get("draft"):
        why.append("it is a draft")
    if label not in {lb["name"] for lb in pr.get("labels", [])}:
        why.append(f"it lacks the review label {label}")
    if pr["user"]["login"] not in authors:
        why.append(f"author {pr['user']['login']} is not on the allowed list")
    head = (pr.get("head") or {}).get("repo") or {}
    if head.get("full_name") != pr["base"]["repo"]["full_name"]:
        why.append("it comes from a fork")
    if pr["base"]["ref"] != BASE_BRANCH:
        why.append(f"it targets {pr['base']['ref']}, not {BASE_BRANCH}")
    # Lines under tests/ do not count; tests are cheap to review.
    lines = sum(fl.get("additions", 0) + fl.get("deletions", 0) for fl in files
                if not fl["filename"].startswith("tests/"))
    if lines > MAX_LINES:
        why.append(f"{lines} changed lines is over the {MAX_LINES} limit")
    if pr["changed_files"] > MAX_FILES:
        why.append(f"{pr['changed_files']} changed files is over the {MAX_FILES} limit")
    if len(files) != pr["changed_files"]:
        why.append("the file list is incomplete")
    for fl in files:
        for name in {fl["filename"], fl.get("previous_filename") or fl["filename"]}:
            if any(fnmatch.fnmatch(name, pat) for pat in SENSITIVE):
                why.append(f"{name} is a sensitive path")
        if fl.get("patch") is None:
            why.append(f"{fl['filename']} has no text diff")
            continue
        for line in fl["patch"].splitlines():
            if line.startswith("+") and RISKY.search(line):
                why.append(f"{fl['filename']} adds a risky line: {line[1:].strip()[:80]}")
    names = [fl["filename"] for fl in files]
    if any(n.startswith("src/") for n in names) and not any(n.startswith("tests/") for n in names):
        why.append("it changes code under src/ without changing tests")
    return why


def main(argv):
    repo, number, action = argv
    pr = json.loads(_gh(["api", f"repos/{repo}/pulls/{number}"]))
    files = json.loads(_gh(["api", f"repos/{repo}/pulls/{number}/files?per_page=100"]))
    events = json.loads(_gh(["api", "--paginate", "--slurp", f"repos/{repo}/issues/{number}/events?per_page=100"]))
    commit = json.loads(_gh(["api", f"repos/{repo}/commits/{pr['head']['sha']}"]))
    authors = {a.strip() for a in os.environ.get("AUTO_MERGE_AUTHORS", "").split(",") if a.strip()}
    label = os.environ.get("AUTO_MERGE_LABEL") or "pl:merge-ready"
    committed = ((commit.get("commit") or {}).get("committer") or {}).get("date")
    why = check(pr, files, action, label, authors, labeled_at(events, label), committed)
    print(f"eligible={'false' if why else 'true'}")
    for reason in why:
        print(f"- {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
