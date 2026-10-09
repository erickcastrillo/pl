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


def check(pr, files, action, label, authors):
    """Return the reasons this pull request may not auto-merge; an empty list means it may."""
    why = []
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
    lines = pr["additions"] + pr["deletions"]
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
    authors = {a.strip() for a in os.environ.get("AUTO_MERGE_AUTHORS", "").split(",") if a.strip()}
    why = check(pr, files, action, os.environ.get("AUTO_MERGE_LABEL") or "pl:merge-ready", authors)
    print(f"eligible={'false' if why else 'true'}")
    for reason in why:
        print(f"- {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
