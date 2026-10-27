"""Answer `gh pr view` and `gh pr list` through REST when the GraphQL budget is low: the same JSON gh prints, for the
fields REST supplies, with no GraphQL points spent. Anything else is left to the caller (None)."""
import json
import os
import re
import subprocess
from urllib.parse import urlencode

from pl import ghquota

FIELDS = ("number title body state url isDraft headRefName headRefOid baseRefName author labels assignees "
          "createdAt updatedAt closedAt mergedAt").split()
REPO = re.compile(r"^(?!\.+/)[\w.-]+/(?!\.+$)[\w.-]+$")   # no "." or ".." segment: the path stays under repos/
PR_URL = re.compile(r"^https://([^/]+)/([^/]+/[^/]+)/pull/(\d+)$")
STATES = ("open", "closed", "merged", "all")
LIST_FLAGS = {"--state": "state", "-s": "state", "--head": "head", "-H": "head", "--base": "base", "-B": "base",
              "--limit": "limit", "-L": "limit"}
REPO_FLAGS = ("--repo", "-R")
TIMES = {"createdAt": "created_at", "updatedAt": "updated_at", "closedAt": "closed_at", "mergedAt": "merged_at"}


def _run(argv, env):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=60, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _flags(rest):
    """The `--flag value` and `--flag=value` pairs of rest and its positionals, or None for a flag without a value."""
    flags, positional, i = {}, [], 0
    while i < len(rest):
        arg = rest[i]
        if arg.startswith("-"):
            name, eq, value = arg.partition("=")
            if not eq:
                i += 1
                if i >= len(rest):
                    return None
                value = rest[i]
            flags[name] = value
        else:
            positional.append(arg)
        i += 1
    return flags, positional


def parse(args):
    """(endpoint, fields, post) for a call REST can answer, else None."""
    if len(args) < 2 or args[0] != "pr" or args[1] not in ("view", "list"):
        return None
    parsed = _flags(args[2:])
    if not parsed:
        return None
    flags, positional = parsed
    view = args[1] == "view"
    allowed = {"--json", *REPO_FLAGS} | (set() if view else set(LIST_FLAGS))
    if set(flags) - allowed or "--json" not in flags:
        return None
    fields = flags["--json"].split(",")
    if not all(f in FIELDS for f in fields):
        return None
    repo = next((flags[f] for f in REPO_FLAGS if f in flags), None)
    if repo is not None and not REPO.match(repo):
        return None
    if view:
        if len(positional) != 1:
            return None
        found = PR_URL.match(positional[0])
        if found:
            if found.group(1) != (os.environ.get("GH_HOST") or "github.com"):
                return None
            repo = found.group(2)
            if not REPO.match(repo):
                return None
        elif not positional[0].isdigit():
            return None
        number = found.group(3) if found else positional[0]
        return f"repos/{repo or '{owner}/{repo}'}/pulls/{number}", fields, None
    if positional:
        return None
    opts = {name: flags[flag] for flag, name in LIST_FLAGS.items() if flag in flags}
    state, limit = opts.get("state", "open"), opts.get("limit", "30")
    if state not in STATES or not limit.isdigit() or not 1 <= int(limit) <= 100:
        return None
    limit = int(limit)
    query = {"state": "closed" if state == "merged" else state, "per_page": 100 if state == "merged" else limit}
    if "head" in opts:
        query["head"] = f"{repo.split('/')[0] if repo else '{owner}'}:{opts['head']}"
    if "base" in opts:
        query["base"] = opts["base"]
    post = (lambda items: [i for i in items if i.get("merged_at")][:limit]) if state == "merged" else None
    return f"repos/{repo or '{owner}/{repo}'}/pulls?{urlencode(query, safe='{}:')}", fields, post


def _value(item, field):
    if field == "state":
        return "MERGED" if item.get("merged_at") else item["state"].upper()
    if field == "url":
        return item["html_url"]
    if field == "isDraft":
        return item["draft"]
    if field == "headRefName":
        return item["head"]["ref"]
    if field == "headRefOid":
        return item["head"]["sha"]
    if field == "baseRefName":
        return item["base"]["ref"]
    if field == "author":
        return {"login": item["user"]["login"]}
    if field == "labels":
        return [{"name": label["name"]} for label in item["labels"]]
    if field == "assignees":
        return [{"login": a["login"]} for a in item["assignees"]]
    if field in TIMES:
        return item.get(TIMES[field])
    return item[field]


def _pr(item, fields):
    return {f: _value(item, f) for f in fields}


def answer(gh, args, env):
    """gh's JSON text for the call, or None when REST cannot answer it (nothing is printed here)."""
    parsed = parse(args)
    if not parsed:
        return None
    endpoint, fields, post = parsed
    done = _run([gh, "api", endpoint], env)
    if done is None or done.returncode != 0:
        return None
    try:
        data = json.loads(done.stdout)
        if isinstance(data, list):
            data = [_pr(i, fields) for i in (post(data) if post else data)]
        else:
            data = _pr(data, fields)
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    return json.dumps(data, separators=(",", ":"))


def below_reserve(led, now):
    """True when the ledger holds a reading of this window under the write reserve."""
    remaining = led.get("remaining")
    return (isinstance(remaining, int) and (led.get("reset") or 0) > now
            and remaining < ghquota.RESERVE * (led.get("limit") or 5000))
