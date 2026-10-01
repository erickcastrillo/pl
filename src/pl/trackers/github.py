"""GitHub trackers: a GitHub Project (stage = Status option) or plain Issues (stage = pl:<column> label).

Card id = "<owner>/<repo>#<number>". The card's metadata is the issue body's last line,
<!-- pl:meta {compact JSON} -->, with ">" escaped so the JSON can never close the comment.
"""
import json
import math
import os
import random
import re
import subprocess
import sys
import threading
import time

from pl import config as C

META_RE = re.compile(r"^<!-- pl:meta (\{.*\}) -->$")
OWNER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")
STAGE_FIELD = "pl stage"
URL_RE = re.compile(r"github\.com/([^/]+/[^/]+)/issues/(\d+)")


_clock = time.time          # tests fake the clock
_LIMIT = {"until": 0.0, "hits": 0}   # no gh call before `until`; hits = rate limits in a row with no reset known
_LIMIT_SEARCH = {"until": 0.0, "hits": 0}   # the same for search calls only (gh search, gh issue list --search)
TOKEN_RE = re.compile(r"\b(gh[pousr]_|github_pat_)[A-Za-z0-9_]+")
BACKOFF = 15 * 60            # the longest wait when GitHub does not say when the limit resets
FIRST_WAIT = 60              # the first such wait; it doubles per hit in a row
LONGEST = 3600               # no back-off, saved or told, runs further ahead than this
_BACKING_OFF = threading.Lock()   # console workers hit the limit together: only the first asks gh when it resets


class RateLimited(SystemExit):
    """GitHub said "rate limit": no gh call from this profile until `until` (epoch seconds); resource "search"
    stops only the search calls. err: gh's own words, tokens masked, first 300 characters."""

    def __init__(self, until, err="", resource="graphql"):
        self.until, self.resource = until, resource
        self.err = TOKEN_RE.sub(r"\1***", " ".join((err or "").split()))[:300]
        what = " (searches only)" if resource == "search" else ""
        self.note = f"GitHub rate-limited{what}, retrying at {time.strftime('%H:%M', time.localtime(until))}"
        if self.err:
            self.note += f": {self.err}"
        super().__init__(f"pl: {self.note}")


def _is_search(args):
    """A call GitHub counts against its search limits: gh search ..., or a list with --search."""
    return args[:1] == ["search"] or "--search" in args


def _mem(resource):
    return _LIMIT_SEARCH if resource == "search" else _LIMIT


def _limit_file(resource="graphql"):
    return C.STATE_DIR / ("gh-ratelimit-search.json" if resource == "search" else "gh-ratelimit.json")


def _save(path, data):
    """Write JSON through a temp file of this process, so two processes never share one."""
    try:
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, path)
    except OSError:
        pass


def _shared(resource="graphql"):
    """(file text, until, hits) of the back-off this profile saved. A missing, garbled or far-off until reads as 0."""
    try:
        raw = _limit_file(resource).read_text()
    except OSError:
        return None, 0.0, 0
    try:
        got = json.loads(raw)
        until, hits = float(got.get("until", 0)), int(got.get("hits", 0))
    except (ValueError, TypeError, AttributeError, OverflowError):
        return raw, 0.0, 0
    return raw, (until if math.isfinite(until) and until <= _clock() + LONGEST else 0.0), hits


def limited(resource="graphql"):
    """The RateLimited error while this profile backs off from GitHub, else None. A search call also waits out
    the general back-off; other calls ignore a search back-off."""
    until = max(_LIMIT["until"], _shared()[1])
    if resource == "search" and _clock() >= until:
        until = max(_LIMIT_SEARCH["until"], _shared("search")[1])
        return RateLimited(until, resource="search") if _clock() < until else None
    return RateLimited(until) if _clock() < until else None


def _passed(resource="graphql"):
    """A gh call worked: start the doubling over and drop the saved back-off, unless another process just renewed it."""
    _mem(resource)["hits"] = 0
    f = _limit_file(resource)
    if not f.exists():
        return
    raw, until, _ = _shared(resource)
    try:
        if until <= _clock() and f.read_text() == raw:
            f.unlink()
    except OSError:
        pass


def _run(args):
    """The one subprocess call: gh with argv only, 60 s timeout."""
    try:
        return subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60, env=C.gh_env())
    except subprocess.TimeoutExpired:
        raise SystemExit(f"pl: gh timed out after 60s: gh {' '.join(args[:2])}") from None
    except FileNotFoundError:
        raise SystemExit("pl: gh is not installed: https://cli.github.com") from None


def _graphql_budget():
    """(remaining, reset epoch) of the GraphQL budget from one cheap REST call; None when gh cannot say."""
    try:
        r = _run(["api", "rate_limit", "--jq", ".resources.graphql.remaining,.resources.graphql.reset"])
        remaining, reset = (int(x) for x in r.stdout.split()[:2]) if not r.returncode else (None, None)
    except (SystemExit, ValueError):
        return None
    return None if remaining is None else (remaining, reset)


def _search_budget():
    """(remaining, reset epoch) of the REST search budget (30 a minute); None when gh cannot say."""
    try:
        r = _run(["api", "rate_limit", "--jq", ".resources.search.remaining,.resources.search.reset"])
        remaining, reset = (int(x) for x in r.stdout.split()[:2]) if not r.returncode else (None, None)
    except (SystemExit, ValueError):
        return None
    return None if remaining is None else (remaining, reset)


def _reset_from_headers():
    """When GitHub lets GraphQL calls through again, from the headers of one tiny GraphQL call (gh prints no
    headers on the call that failed). /rate_limit can misreport the GraphQL budget, so these headers come first."""
    try:
        r = _run(["api", "-i", "graphql", "-f", "query={viewer{login}}"])
    except SystemExit:
        return None
    h = {}
    for line in r.stdout.splitlines()[1:]:
        if not line.strip():
            break
        k, _, v = line.partition(":")
        h[k.strip().lower()] = v.strip()
    now = _clock()
    try:
        if "retry-after" in h:
            return now + min(int(h["retry-after"]), LONGEST)
        if h.get("x-ratelimit-remaining") == "0" and int(h.get("x-ratelimit-reset") or 0) > now:
            return float(h["x-ratelimit-reset"])
    except ValueError:
        pass
    return None


def back_off(err, budget=None, told=None, resource="graphql"):
    """For a failed gh call's stderr: on a rate limit (primary, secondary or abuse), stop gh calls from every
    process of this profile until GitHub's reset, else for 1, 2, 4... minutes (at most 15, with jitter), and
    return the RateLimited error; otherwise None. told: the reset the header probe already gave.
    resource "search": a search call failed, so only search calls stop, until the search budget's reset."""
    low = (err or "").lower()
    if "rate limit" not in low and "abuse" not in low:
        return None
    with _BACKING_OFF:
        if hit := limited(resource):   # another thread's call failed first and saved the back-off
            return RateLimited(hit.until, err, hit.resource)
        return _back_off(err, budget, told, resource)


def _back_off(err, budget, told, resource):
    now = _clock()
    mem = _mem(resource)
    _, saved, hits = _shared(resource)
    hits = max(hits, mem["hits"])
    if resource == "search":
        spent = _search_budget()
        until = float(spent[1]) if spent and spent[0] == 0 and spent[1] > now else None
    else:
        until = told or (None if budget else _reset_from_headers())
    if until is None and resource != "search":
        budget = budget or _graphql_budget()
        if budget and budget[0] == 0 and budget[1] > now:
            until = float(budget[1])
    if until is None:
        until = now + min(BACKOFF, FIRST_WAIT * 2 ** min(hits, 20)) * random.uniform(1, 1.25)
        hits += 1
    until = max(min(until, now + LONGEST), saved)   # a short wait never cuts short a longer one another process saved
    mem.update(until=until, hits=hits)
    _save(_limit_file(resource), {"until": until, "hits": hits})
    return RateLimited(until, err, resource)


def _gh(args):
    """The one place pl runs gh: argv only, 60 s timeout, stdout back. No call at all while rate limited."""
    resource = "search" if _is_search(args) else "graphql"
    if hit := limited(resource):
        raise hit
    r = _run(args)
    if r.returncode:
        err = r.stderr.strip()
        if hit := back_off(err, resource=resource):
            raise hit
        if "unknown owner type" in err.lower():   # gh's owner lookup also fails this way when the budget is spent
            if (told := _reset_from_headers()) is not None:   # /rate_limit can misreport: the headers come first
                raise back_off(f"rate limit: {err}", told=told)
            budget = _graphql_budget()
            if budget and budget[0] == 0 and (hit := back_off(f"rate limit: {err}", budget)):
                raise hit
            raise SystemExit(f"pl: gh {' '.join(args[:2])}: {err} (check [tracker] owner, and that gh's sign-in can "
                             "see the project: gh auth status)")
        if "scope" in err.lower() and "project" in err.lower():
            raise SystemExit("pl: gh is missing the project scope: run: gh auth refresh -s project")
        e = SystemExit(f"pl: gh {' '.join(args[:2])}: {err}")
        e.out = r.stdout   # a GraphQL error still carries the data GitHub could answer
        raise e
    _passed(resource)
    return r.stdout


def _pack(description, metadata):
    meta = json.dumps(metadata or {}, separators=(",", ":"), sort_keys=True).replace(">", "\\u003e")
    return f"{description}\n<!-- pl:meta {meta} -->"


def _unpack(body):
    body = body or ""
    head, _, last = body.rstrip().rpartition("\n")
    m = META_RE.match(last)
    if not m:
        return body, {}
    try:
        meta = json.loads(m.group(1))
    except ValueError:
        return body, {}
    if not isinstance(meta, dict):
        return body, {}
    return (head[:-1] if head.endswith("\r") else head), meta


def _split(item_id):
    repo, _, num = item_id.rpartition("#")
    return repo, num


def _names(xs):
    return [x["name"] if isinstance(x, dict) else x for x in xs or []]


def _logins(xs):
    return [x["login"] if isinstance(x, dict) else x for x in xs or []]


def _bad_label(x):
    """gh gets labels as argv and splits on commas: blank, flag-like or comma names never reach it."""
    return not isinstance(x, str) or not x.strip() or x.startswith("-") or "," in x


class _GitHub:
    """Shared issue plumbing; subclasses say where the stage lives."""
    stage_prefix = None

    def __init__(self, cfg):
        self.repo = cfg.get("repo")
        self._columns = None
        self._read = None      # (time, cards, as of): one board read serves a whole refresh or pass; a write drops it
        self._labels = {}      # repo -> label names; listed once per tracker (trackers.get keeps one per process)

    def _usable(self, repo, labels):
        """The labels gh can add: bad names dropped, missing ones created (an existing label is never changed).
        A label that cannot be created is left off. Every drop prints one warning line."""
        out = []
        for lab in labels:
            if _bad_label(lab):
                print(f"pl: warning: label {lab!r} left off: blank, starts with '-' or has a comma", file=sys.stderr)
                continue
            if repo not in self._labels:
                self._labels[repo] = set(_names(json.loads(
                    _gh(["label", "list", "--repo", repo, "--json", "name", "--limit", "1000"]) or "[]")))
            if lab not in self._labels[repo]:
                try:
                    _gh(["label", "create", lab, "--repo", repo, "--color", "EDEDED", "--description", "added by pl"])
                except SystemExit as e:
                    why = (str(e).splitlines() or [""])[0]
                    print(f"pl: warning: label {lab!r} left off: it could not be created on {repo}: {why}", file=sys.stderr)
                    continue
                self._labels[repo].add(lab)
            out.append(lab)
        return out

    def _card(self, repo, num, title, body, labels, assignees, url, updated_at, list_id):
        desc, meta = _unpack(body)
        labels = _names(labels)
        tags = [x for x in labels if not (self.stage_prefix and x.startswith(self.stage_prefix))]
        return {"id": f"{repo}#{num}", "title": title, "description": desc, "tags": tags, "metadata": meta,
                "list_id": list_id, "updated_at": (updated_at or "").replace("Z", "+00:00"),
                "assigned_to": (_logins(assignees) or [None])[0], "url": url, "labels": labels}

    def _col(self, name):
        if name not in self.columns():
            raise SystemExit(self._missing(name))
        return self.columns()[name]

    def _new_issue(self, repo, title, description, labels, metadata, assigned_to):
        args = ["issue", "create", "--repo", repo, "--title", title, "--body", _pack(description, metadata)]
        for lab in labels:
            args += ["--label", lab]
        if assigned_to:
            args += ["--assignee", assigned_to]
        url = _gh(args).strip().splitlines()[-1]
        m = URL_RE.search(url)
        if not m:
            raise SystemExit(f"pl: gh issue create gave no issue URL: {url[:160]}")
        return m.group(1), m.group(2), url

    def _move(self, cur, column):
        raise NotImplementedError

    def update(self, item_id, *, verify=True, **fields):
        """Read-modify-write of the issue body; verify re-reads and asserts every sent field persisted.

        verify=False writes the given fields without the persistence check. The body holds both the
        description and the metadata, so changing only one of them still needs the current other one.
        """
        self._read = None   # the pre-read merges metadata: it must be fresh, and the write makes any cached list stale
        repo, num = _split(item_id)
        if "list_id" in fields:   # pl's commands move with list_id=col_id(...): here that is the column's option
            lid = fields.pop("list_id")
            fields["column"] = next((n for n, i in self.columns().items() if i == lid), None)
            if fields["column"] is None:
                raise SystemExit(f"pl: {item_id}: no column has id {lid!r}")
        body_half = ("description" in fields) != ("metadata" in fields)
        cur = self.card(item_id) if verify or body_half or "tags" in fields or "column" in fields else None
        column = fields.pop("column", None)
        args = []
        if "title" in fields:
            args += ["--title", fields["title"]]
        if "description" in fields or "metadata" in fields:
            desc = fields["description"] if "description" in fields else cur["description"]
            meta = dict(cur["metadata"]) if cur else {}
            if "metadata" in fields:
                meta = {**meta, **fields["metadata"]} if verify else dict(fields["metadata"])
            args += ["--body", _pack(desc, meta)]
        if "tags" in fields:
            old = set(cur["tags"])
            add = set(self._usable(repo, sorted(set(fields["tags"]) - old)))
            fields["tags"] = [x for x in fields["tags"] if x in old or x in add]
            new = set(fields["tags"])
            if new - old:
                args += ["--add-label", ",".join(sorted(new - old))]
            if old - new:
                args += ["--remove-label", ",".join(sorted(old - new))]
        if fields.get("assigned_to"):
            args += ["--add-assignee", fields["assigned_to"]]
        if args:
            _gh(["issue", "edit", num, "--repo", repo, *args])
        if column is not None:
            self._move(cur, column)
        self._read = None
        if not verify:
            return {"id": item_id}
        got = self.card(item_id)
        for mk, mv in (fields.get("metadata") or {}).items():
            if got["metadata"].get(mk) != mv:
                raise SystemExit(f"pl: metadata.{mk} did not persist on {item_id}")
        for k, v in fields.items():
            if k == "metadata":
                continue
            g = got.get(k)
            if (sorted(g or []) != sorted(v or [])) if k == "tags" else ((g or "") != (v or "")):
                raise SystemExit(f"pl: field {k} did not persist on {item_id}")
        if column is not None and got["list_id"] != self.columns()[column]:
            raise SystemExit(f"pl: field column did not persist on {item_id}")
        return got

    def url(self, item_id):
        repo, num = _split(item_id)
        return f"https://github.com/{repo}/issues/{num}"


def open_issues(repo, search):
    """One cheap call: up to 30 open issues of repo that match a GitHub search, without their bodies (a GraphQL
    search; 30 newest-updated is plenty for an intake that runs every 5 minutes)."""
    return json.loads(_gh(["issue", "list", "--repo", repo, "--state", "open", "--json",
                           "number,title,labels,assignees,updatedAt", "--limit", "30", "--search", search]) or "[]")


def stage_field(owner, number, name):
    """The project's field called name, as gh's field-list gives it ({id, name, type, options}), or None."""
    j = json.loads(_gh(["project", "field-list", str(number), "--owner", owner, "--limit", "100", "--format", "json"]))
    return next((f for f in j.get("fields", []) if f.get("name") == name), None)


def create_stage_field(owner, number, name, columns):
    """Add a single-select field called name with one option per column. Never touches another field."""
    if bad := [c for c in columns if "," in c]:
        raise SystemExit(f"pl: gh cannot create an option whose name has a comma: {', '.join(map(repr, bad))}; rename it")
    _gh(["project", "field-create", str(number), "--owner", owner, "--name", name, "--data-type", "SINGLE_SELECT",
         "--single-select-options", ",".join(columns)])


def project_url(owner, number):
    """The project's web address, or the owner's projects page if gh cannot say."""
    try:
        return json.loads(_gh(["project", "view", str(number), "--owner", owner, "--format", "json"]))["url"]
    except (SystemExit, ValueError, KeyError, TypeError):
        return f"https://github.com/{owner}?tab=projects (project {number})"


class GqlEnum(str):
    """A GraphQL enum value (BOARD_LAYOUT, GRAY, ...): written bare, not quoted."""


ENUM_RE = re.compile(r"[A-Z][A-Z0-9_]*")
PROJECT_READ = ("{id views(first:50){nodes{id number name layout filter}} fields(first:100){nodes{"
                "... on ProjectV2FieldCommon{id name dataType} "
                "... on ProjectV2SingleSelectField{options{id name color description}}}}}")


def _lit(v):
    """A GraphQL literal. Strings are JSON-escaped, so card or field text can never end the query early."""
    if isinstance(v, GqlEnum):
        if not ENUM_RE.fullmatch(v):
            raise SystemExit(f"pl: not a GraphQL enum value: {v!r}")
        return str(v)
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return json.dumps(v)
    if isinstance(v, (list, tuple)):
        return "[" + ",".join(_lit(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ",".join(f"{k}:{_lit(x)}" for k, x in v.items()) + "}"
    raise SystemExit(f"pl: cannot send {type(v).__name__} to GraphQL")


def _graphql(query):
    """One `gh api graphql -f query=...` call; gh exits non-zero on a GraphQL error, which _gh turns into SystemExit."""
    return json.loads(_gh(["api", "graphql", "-f", "query=" + query]) or "{}").get("data") or {}


def _mutate(name, inp):
    _graphql(f"mutation{{{name}(input:{_lit(inp)}){{clientMutationId}}}}")


def project_graph(owner, number):
    """The project's node id, views and fields (single-select ones with their options), as a user's then an org's."""
    err = None
    for kind in ("user", "organization"):
        try:
            p = (_graphql(f"query{{{kind}(login:{_lit(owner)}){{projectV2(number:{int(number)}){PROJECT_READ}}}}}")
                 .get(kind) or {}).get("projectV2")
        except SystemExit as e:
            err = e
            continue
        if p:
            return {"id": p["id"], "views": [v for v in p["views"]["nodes"] if v],
                    "fields": [f for f in p["fields"]["nodes"] if f]}
    raise err or SystemExit(f"pl: no project {number} owned by {owner}")


def create_view(project_id, name, layout):
    """A new view; GitHub takes no filter here, so set it after with update_view."""
    _mutate("createProjectV2View", {"projectId": project_id, "name": name, "layout": GqlEnum(layout)})


def update_view(view_id, **fields):
    """Change a view's name, layout or filter."""
    _mutate("updateProjectV2View", {"viewId": view_id, **{k: GqlEnum(v) if k == "layout" else v for k, v in fields.items()}})


def set_field_options(field_id, options):
    """Replace a single-select field's options. An option passed with its id keeps that id; one without id is new."""
    _mutate("updateProjectV2Field", {"fieldId": field_id, "singleSelectOptions": [
        {k: GqlEnum(o[k]) if k == "color" else o[k] for k in ("id", "name", "color", "description") if k in o}
        for o in options]})


def create_iteration_field(project_id, name, start, days):
    """An ITERATION field whose first iteration, "Sprint 1", starts on start (YYYY-MM-DD) and lasts days."""
    _mutate("createProjectV2Field", {"projectId": project_id, "dataType": GqlEnum("ITERATION"), "name": name,
                                     "iterationConfiguration": {"startDate": start, "duration": days, "iterations": [
                                         {"title": "Sprint 1", "startDate": start, "duration": days}]}})


READ_TTL = 20      # seconds one board read is reused within this process
FIRST_LIMIT = 50   # items asked for on a board's first read in a process; later reads ask for what it held plus 20
_LIMITS = {}       # (owner, number) -> item-list --limit that fits the board
_KNOWN = {}        # ("field", owner, number, name) -> stage field; ("id", owner, number) -> project id. Kept for the
                   # process (across dispatcher passes); a column that is not in it reads the field again.
_STAMPS = {}       # card id -> (what the card looked like, its issue's updatedAt), kept for the process
LAG = 15 * 60      # seconds a card pl created is looked up by issue while GitHub's project listing lacks it


def forget():
    """Drop what this process remembers about projects (trackers.reset() with no kind calls it)."""
    _KNOWN.clear()
    _LIMITS.clear()
    _STAMPS.clear()


def _print(card):
    """What a card looks like on the board, for telling whether it changed since the last read."""
    return json.dumps([card[k] for k in ("list_id", "title", "description", "labels", "metadata", "assigned_to")],
                      sort_keys=True)


def _recent_file():
    return C.STATE_DIR / "gh-recent.json"


def _recent(keep=None):
    """Cards pl created in the last LAG seconds: [{id, item_id, column, at}]. keep= saves that list instead."""
    path = _recent_file()
    if keep is None:
        try:
            got = json.loads(path.read_text())
        except (OSError, ValueError):
            return []
        return [r for r in got if isinstance(r, dict) and _clock() - r.get("at", 0) < LAG] if isinstance(got, list) else []
    _save(path, keep)
    return keep


ISSUE_READ = ("{{number title body url updatedAt labels(first:50){{nodes{{name}}}} assignees(first:10){{nodes{{login}}}} "
              "projectItems(first:20){{nodes{{id fieldValueByName(name:{field}){{... on ProjectV2ItemFieldSingleSelectValue"
              "{{name optionId}}}} project{{id number owner{{... on User{{login}} ... on Organization{{login}}}} "
              "field(name:{field}){{... on ProjectV2SingleSelectField{{id options{{id name}}}}}}}}}}}}}}")


class GitHubProject(_GitHub):
    """Cards are the issue items of a GitHub Project; the stage is the Status field's option."""

    def __init__(self, cfg):
        super().__init__(cfg)
        self.owner = cfg["owner"]
        self.number = str(cfg["number"])
        if "status_field" in cfg and not str(cfg["status_field"] or "").strip():
            raise SystemExit("pl: [tracker] status_field is blank; name the project's stage field or remove the key")
        self.status_field = cfg.get("status_field") or "Status"
        self.want = list(cfg.get("columns") or C.COLUMNS)

    def _missing(self, name):
        return f"pl: add a '{name}' option to the {self.status_field} field"

    @staticmethod
    def create_project(owner, title, columns):
        """Create a GitHub Project with a "pl stage" single-select field; returns dict(number, url, status_field).
        pl never edits the built-in Status field. If the field cannot be added, the error names the new project's URL."""
        if not (isinstance(owner, str) and OWNER_RE.fullmatch(owner)):
            raise SystemExit(f"pl: bad GitHub owner {owner!r}: letters, digits and '-', at most 39, not starting with '-'")
        title = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", str(title or "")).strip()
        if not title:
            raise SystemExit("pl: the project title must not be empty")
        j = json.loads(_gh(["project", "create", "--owner", owner, "--title", title, "--format", "json"]))
        number, url = str(j["number"]), j.get("url") or f"https://github.com/{owner} project {j['number']}"
        try:
            create_stage_field(owner, number, STAGE_FIELD, columns)
        except SystemExit as e:
            raise SystemExit(f"{e}\npl: the project was created but has no '{STAGE_FIELD}' field; nothing was saved. "
                             f"Delete it or add the field by hand: {url}") from None
        return {"number": j["number"], "url": url, "status_field": STAGE_FIELD}

    def _status(self):
        key = ("field", self.owner, self.number, self.status_field)
        if key not in _KNOWN:
            j = json.loads(_gh(["project", "field-list", self.number, "--owner", self.owner, "--limit", "100",
                                "--format", "json"]))
            f = next((f for f in j.get("fields", []) if f.get("name") == self.status_field), None)
            if f is None:
                raise SystemExit(f"pl: project {self.owner}/{self.number} has no {self.status_field!r} field")
            _KNOWN[key] = f
        return _KNOWN[key]

    def _refield(self):
        """GitHub has a column the remembered field lacks: read the field again."""
        _KNOWN.pop(("field", self.owner, self.number, self.status_field), None)
        self._columns = None

    def _project_id(self):
        key = ("id", self.owner, self.number)
        if key not in _KNOWN:
            _KNOWN[key] = json.loads(_gh(["project", "view", self.number, "--owner", self.owner, "--format", "json"]))["id"]
        return _KNOWN[key]

    def columns(self):
        if self._columns is None:
            self._columns = {o["name"]: o["id"] for o in self._status().get("options", [])}
        return self._columns

    def _col(self, name):
        if name not in self.columns():
            self._refield()
        return super()._col(name)

    def _issues(self, ids):
        """{card id: issue node with its project items} from one GraphQL query, whatever the project listing says."""
        parts = []
        for n, cid in enumerate(ids):
            repo, num = _split(cid)
            owner, _, name = repo.partition("/")
            parts.append(f"i{n}:repository(owner:{_lit(owner)},name:{_lit(name)}){{issue(number:{int(num)})"
                         f"{ISSUE_READ.format(field=_lit(self.status_field))}}}")
        data = _graphql("query{" + " ".join(parts) + "}")
        return {cid: (data.get(f"i{n}") or {}).get("issue") for n, cid in enumerate(ids)}

    def _mine(self, issue):
        """The issue's item on this project, or None."""
        return next((it for it in ((issue or {}).get("projectItems") or {}).get("nodes") or []
                     if it and str((it.get("project") or {}).get("number")) == self.number
                     and ((it["project"].get("owner") or {}).get("login") or "").lower() == self.owner.lower()), None)

    def ref_id(self, ref):
        """The card id for an issue number or card id, from the text alone."""
        ref = str(ref).strip()
        cid = ref if "#" in ref.strip("#") else f"{self.repo}#{ref.lstrip('#')}"
        repo, num = _split(cid)
        if not (re.fullmatch(r"[\w.-]+/[\w.-]+", repo or "") and num.isdigit()):
            raise SystemExit(f"pl: not an issue number or card id: {ref!r}")
        return cid

    def move(self, ref, column):
        """Set a card's stage from its issue number or card id: one issue lookup and one item-edit, no board listing."""
        cid = self.ref_id(ref)
        it = self._mine(self._issues([cid])[cid])
        if it is None:
            raise SystemExit(f"pl: {cid} is not on project {self.owner}/{self.number}")
        field = it["project"].get("field") or {}
        opt = next((o["id"] for o in field.get("options") or [] if o.get("name") == column), None)
        if opt is None:
            raise SystemExit(self._missing(column))
        self._read = None
        _gh(["project", "item-edit", "--id", it["id"], "--project-id", it["project"]["id"], "--field-id", field["id"],
             "--single-select-option-id", opt])
        return cid

    def _items(self):
        if self._read and _clock() - self._read[0] < READ_TTL:
            return [dict(c) for c in self._read[1]]
        start = _clock()
        key = (self.owner, self.number)
        limit = _LIMITS.get(key, FIRST_LIMIT)
        while True:   # a bigger board than asked for: ask again for all of it
            j = json.loads(_gh(["project", "item-list", self.number, "--owner", self.owner, "--format", "json",
                                "--limit", str(limit)]))
            if len(j.get("items", [])) < limit:
                break
            limit = max(int(j.get("totalCount") or 0) + 1, limit * 2)
        _LIMITS[key] = len(j.get("items", [])) + 20   # GitHub charges per item asked for
        by_name = self.columns()
        key = self.status_field[:1].lower() + self.status_field[1:]
        if any(it.get(key) and it.get(key) not in by_name for it in j.get("items", [])):
            self._refield()
            by_name = self.columns()
        out = []
        for it in j.get("items", []):
            c = it.get("content") or {}
            m = URL_RE.search(c.get("url") or "")
            if c.get("type") != "Issue" or not m:
                continue
            card = self._card(m.group(1), m.group(2), c.get("title") or it.get("title"), c.get("body"),
                              it.get("labels"), it.get("assignees"), c["url"], c.get("updatedAt"),
                              by_name.get(it.get(key)))
            card["item_id"] = it["id"]
            out.append(card)
        self._stamp(out)
        out += self._lagging({c["id"] for c in out})
        self._read = (_clock(), out, start)
        return [dict(c) for c in out]

    def _stamp(self, cards):
        """Set updated_at, which gh's item-list lacks: one aliased GraphQL query for the cards that are new to this
        process or changed since its last read (column, title, body, labels, assignee); the rest keep their stamp."""
        todo = [c for c in cards if c["updated_at"] == "" and _STAMPS.get(c["id"], (None,))[0] != _print(c)]
        for part in (todo[i:i + 100] for i in range(0, len(todo), 100)):
            q = []
            for n, c in enumerate(part):
                owner, _, name = _split(c["id"])[0].partition("/")
                q.append(f"i{n}:repository(owner:{_lit(owner)},name:{_lit(name)}){{issue(number:{int(_split(c['id'])[1])})"
                         "{updatedAt}}")
            try:
                data = _graphql("query{" + " ".join(q) + "}")
            except RateLimited:
                raise
            except SystemExit as e:   # one issue gone fails the query: stamp what came back; the rest are asked again
                try:
                    data = json.loads(getattr(e, "out", "") or "{}").get("data") or {}
                except ValueError:
                    continue
            for n, c in enumerate(part):
                at = ((data.get(f"i{n}") or {}).get("issue") or {}).get("updatedAt")
                if at:
                    _STAMPS[c["id"]] = (_print(c), at.replace("Z", "+00:00"))
        for c in cards:
            if c["updated_at"] == "" and _STAMPS.get(c["id"], (None,))[0] == _print(c):
                c["updated_at"] = _STAMPS[c["id"]][1]

    def _lagging(self, listed):
        """Cards pl created that GitHub's project listing does not show yet, read by issue; listed ones are forgotten."""
        recent = _recent()
        keep = [r for r in recent if r.get("id") not in listed]
        if keep != recent:
            _recent(keep)
        if not keep:
            return []
        try:
            found = self._issues([r["id"] for r in keep])
        except RateLimited:
            raise
        except SystemExit:   # an issue deleted or moved away fails the whole lookup: these are only a lag aid
            _recent([])
            return []
        if gone := [cid for cid, issue in found.items() if issue is None]:
            _recent([r for r in keep if r["id"] not in gone])
        out = []
        for cid, issue in found.items():
            it = self._mine(issue)
            if it is None:
                continue
            repo, num = _split(cid)
            card = self._card(repo, num, issue.get("title"), issue.get("body"), (issue.get("labels") or {}).get("nodes"),
                              (issue.get("assignees") or {}).get("nodes"), issue.get("url"), issue.get("updatedAt"),
                              (it.get("fieldValueByName") or {}).get("optionId"))
            card["item_id"] = it["id"]
            out.append(card)
        return out

    def cards(self, query=None):
        return self._items()

    def seed(self, cards, at, until):
        """A listing another process of the profile read at `at` (board.cards): card(id) finds cards in it for READ_TTL,
        and never past `until`, when the save itself stops being good."""
        self._read = (min(_clock(), until - READ_TTL), [dict(c) for c in cards], at)

    def card(self, item_id):
        hit = next((c for c in self._items() if c["id"] == item_id), None)
        if hit is None:
            raise SystemExit(f"pl: no card {item_id} in project {self.owner}/{self.number}")
        return hit

    def _set_status(self, project_item, column, retry=True):
        try:
            _gh(["project", "item-edit", "--id", project_item, "--project-id", self._project_id(),
                 "--field-id", self._status()["id"], "--single-select-option-id", self._col(column)])
        except RateLimited:
            raise
        except SystemExit:
            if not retry:
                raise
            self._refield()   # the remembered field may be gone (column deleted and re-added): read it once more
            self._set_status(project_item, column, retry=False)

    def _move(self, cur, column):
        self._set_status(cur["item_id"], column)

    def create(self, column, *, title, description="", tags=(), metadata=None, assigned_to=None):
        if not self.repo:
            raise SystemExit("pl: set repo in the github-project tracker settings to create cards")
        self._col(column)
        tags = self._usable(self.repo, tags)
        repo, num, url = self._new_issue(self.repo, title, description, tags, metadata, assigned_to)
        self._read = None
        j = json.loads(_gh(["project", "item-add", self.number, "--owner", self.owner, "--url", url,
                            "--format", "json"]))
        self._set_status(j["id"], column)
        _recent([*_recent(), {"id": f"{repo}#{num}", "at": _clock()}])
        return {"id": f"{repo}#{num}", "title": title, "description": description, "tags": tags,
                "metadata": dict(metadata or {}), "list_id": self.columns()[column], "updated_at": "",
                "assigned_to": assigned_to, "url": url, "item_id": j["id"]}

    def adopt(self, item_id, column, *, description, metadata, assigned_to=None, remove_label=None):
        """Put an existing issue on the project in `column` with pl's body: the issue becomes the card, no second one."""
        self._col(column)
        repo, num = _split(item_id)
        args = ["issue", "edit", num, "--repo", repo, "--body", _pack(description, metadata)]
        if assigned_to:
            args += ["--add-assignee", assigned_to]
        if remove_label:
            args += ["--remove-label", remove_label]
        _gh(args)
        self._read = None
        j = json.loads(_gh(["project", "item-add", self.number, "--owner", self.owner, "--url", self.url(item_id),
                            "--format", "json"]))
        self._set_status(j["id"], column)
        _recent([*_recent(), {"id": item_id, "at": _clock()}])
        return {"id": item_id, "item_id": j["id"], "metadata": dict(metadata)}

    def delete(self, item_id):
        cur = self.card(item_id)
        self._read = None
        _recent([r for r in _recent() if r.get("id") != item_id])
        _gh(["project", "item-delete", self.number, "--owner", self.owner, "--id", cur["item_id"]])
        repo, num = _split(item_id)
        _gh(["issue", "close", num, "--repo", repo])

    def ensure_column(self, name, *, description=""):
        return self._col(name)

    def test(self):
        self._refield()   # a connection test reads GitHub now, not what this process remembers
        have = self.columns()
        missing = [c for c in self.want if c not in have]
        where = f"github-project {self.owner}/{self.number}"
        if missing:
            return f"{where}: missing {self.status_field} options: {', '.join(missing)} (add them in GitHub)"
        return f"ok: {where}, {len(have)} {self.status_field} options"


class GitHubIssues(_GitHub):
    """Cards are the open issues of one repo; the stage is a pl:<column> label."""
    FIELDS = "number,title,body,labels,assignees,url,updatedAt"

    def __init__(self, cfg):
        super().__init__(cfg)
        self.stage_prefix = cfg.get("label_prefix") or "pl:"
        self.names = list(cfg.get("columns") or C.COLUMNS)

    def _missing(self, name):
        return f"pl: {self.repo} has no column {name!r} (valid: {', '.join(self.names)})"

    def columns(self):
        return {n: n for n in self.names}

    def _from(self, j):
        stage = [x[len(self.stage_prefix):] for x in _names(j.get("labels")) if x.startswith(self.stage_prefix)]
        repo, num = URL_RE.search(j["url"]).groups()
        return self._card(repo, num, j["title"], j.get("body"), j.get("labels"), j.get("assignees"), j["url"],
                          j.get("updatedAt"), stage[0] if stage else None)

    def cards(self, query=None):
        j = json.loads(_gh(["issue", "list", "--repo", self.repo, "--state", "open", "--json", self.FIELDS,
                            "--limit", "500"]))
        return [self._from(i) for i in j]

    def card(self, item_id):
        repo, num = _split(item_id)
        return self._from(json.loads(_gh(["issue", "view", num, "--repo", repo, "--json", self.FIELDS])))

    def ensure_column(self, name, *, description=""):
        self._col(name)
        args = ["label", "create", self.stage_prefix + name, "--repo", self.repo, "--force"]
        if description:
            args += ["--description", description]
        _gh(args)
        return name

    def _move(self, cur, column):
        self.ensure_column(column)
        repo, num = _split(cur["id"])
        new = self.stage_prefix + column
        old = [x for x in cur["labels"] if x.startswith(self.stage_prefix) and x != new]
        args = ["issue", "edit", num, "--repo", repo, "--add-label", new]
        if old:
            args += ["--remove-label", ",".join(old)]
        _gh(args)

    def create(self, column, *, title, description="", tags=(), metadata=None, assigned_to=None):
        self.ensure_column(column)
        tags = self._usable(self.repo, tags)
        repo, num, url = self._new_issue(self.repo, title, description, [self.stage_prefix + column, *tags], metadata,
                                         assigned_to)
        return {"id": f"{repo}#{num}", "title": title, "description": description, "tags": tags,
                "metadata": dict(metadata or {}), "list_id": column, "updated_at": "",
                "assigned_to": assigned_to, "url": url}

    def delete(self, item_id):
        repo, num = _split(item_id)
        _gh(["issue", "close", num, "--repo", repo])

    def test(self):
        _gh(["label", "list", "--repo", self.repo, "--json", "name", "--limit", "1"])
        return f"ok: github-issues {self.repo}, {len(self.names)} columns ({self.stage_prefix}<column> labels)"
