"""Require a security review label on pull requests touching security-sensitive paths.

Run by .github/workflows/security-label.yml from the base branch, never from the pull request,
so a pull request cannot change the rules that judge it. It reads the pull request through the
GitHub API only and never runs the pull request's code.

Usage: security_label_check.py OWNER/REPO NUMBER ACTION
"""
import fnmatch
import json
from pathlib import Path
import sys

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from auto_merge_check import SECURITY_PATHS, _gh, _when, labeled_at  # noqa: E402

SECURITY_LABEL = "pl:security-reviewed"


def touches_security(files):
    for fl in files:
        for name in {fl["filename"], fl.get("previous_filename") or fl["filename"]}:
            if any(fnmatch.fnmatch(name, pat) for pat in SECURITY_PATHS):
                return True
    return False


def check(pr, files, action="labeled", label=SECURITY_LABEL, labeled_at=None, committed_at=None):
    """Return reasons this pull request fails the security label check; empty list means it passes."""
    if not touches_security(files):
        return []
    why = []
    labels = {lb["name"] for lb in pr.get("labels", [])}
    if label not in labels:
        why.append(f"it touches security-sensitive paths but lacks the required label {label}")
    elif not labeled_at or not committed_at:
        why.append("there is no record of when the security review label was added or the last commit was made")
    elif _when(labeled_at) <= _when(committed_at):
        why.append("the security review label was added before the last commit; it needs a fresh review")
    if action == "synchronize":
        why.append("new commits arrived after the security review; it needs a fresh review label")
    return why


def main(argv):
    repo, number, action = argv[:3]
    pr = json.loads(_gh(["api", f"repos/{repo}/pulls/{number}"]))
    files = json.loads(_gh(["api", f"repos/{repo}/pulls/{number}/files?per_page=100"]))
    events = json.loads(_gh(["api", "--paginate", "--slurp", f"repos/{repo}/issues/{number}/events?per_page=100"]))
    commit = json.loads(_gh(["api", f"repos/{repo}/commits/{pr['head']['sha']}"]))
    committed = ((commit.get("commit") or {}).get("committer") or {}).get("date")
    why = check(pr, files, action=action, labeled_at=labeled_at(events, SECURITY_LABEL), committed_at=committed)
    if why:
        for reason in why:
            print(f"- {reason}", file=sys.stderr)
        return 1
    print("Security label check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
