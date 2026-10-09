"""Fail a pull request that is too big to review well.

Usage: pr_size_check.py BASE_REF
Runs git diff --numstat BASE_REF...HEAD, prints ok or the reasons, and exits 1 when over a limit.
Lines under tests/ do not count toward the line limit. Every file counts toward the file limit.
A binary file counts as a file with no lines.
"""
import subprocess
import sys

MAX_LINES = 300
MAX_FILES = 10


def _git(args):
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def check(numstat):
    """Return the reasons this diff is too big; an empty list means it is fine."""
    lines = files = 0
    for row in numstat.splitlines():
        if not row.strip():
            continue
        added, deleted, name = row.split("\t", 2)
        files += 1
        if name.startswith("tests/") or added == "-":
            continue
        lines += int(added) + int(deleted)
    why = []
    if lines > MAX_LINES:
        why.append(f"{lines} changed lines outside tests/ is over the {MAX_LINES} limit")
    if files > MAX_FILES:
        why.append(f"{files} changed files is over the {MAX_FILES} limit")
    return why


def main(argv):
    why = check(_git(["diff", "--numstat", f"{argv[0]}...HEAD"]))
    print("\n".join(why) if why else "ok")
    return 1 if why else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
