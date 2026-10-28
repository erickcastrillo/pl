"""Vikunja board tracker: one project's Kanban view is the board (buckets = columns, tasks = cards)."""
import json
import urllib.error
import urllib.parse
import urllib.request

from pl import util
from pl.trackers import mcp, metablock

PAGE = 200      # tasks per bucket per request
TIMEOUT = 15


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class Vikunja:
    def __init__(self, cfg):
        self.url_base = cfg["url"].rstrip("/")
        self._token_ref = cfg["token"]
        self.project = int(cfg["project_id"])
        self.view = int(cfg["view_id"])
        self._columns = None
        self._labels = {}
        self._emails = {}
        self._me = None

    def _open(self, method, path, body=None):
        token = mcp._resolve(self._token_ref, [])
        req = urllib.request.Request(
            f"{self.url_base}/api/v1{path}", method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
        with opener.open(req, timeout=TIMEOUT) as r:
            return r.read()

    def _http(self, method, path, body=None):
        try:
            raw = self._open(method, path, body)
        except urllib.error.HTTPError as e:
            raise SystemExit(util.mask(f"pl: vikunja: {method} {path}: {e.code}")) from None
        except urllib.error.URLError as e:
            raise SystemExit(util.mask(f"pl: vikunja: {method} {path}: {e.reason}")) from None
        return json.loads(raw) if raw.strip() else None

    def _view_path(self):
        return f"/projects/{self.project}/views/{self.view}"

    @staticmethod
    def _tid(item_id):
        return int(str(item_id).rpartition("#")[2])

    def _own_user(self):
        if self._me is None:
            self._me = self._http("GET", "/user")
            if self._me.get("email"):   # Vikunja leaves it out unless the user made it visible
                self._emails[self._me["id"]] = self._me["email"]
        return self._me

    def _user_id(self, email):
        me = self._own_user()
        if (me.get("email") or "").lower() == email.lower():
            return me["id"]
        found = self._http("GET", f"/users?s={urllib.parse.quote(email)}") or []
        for u in found:
            if (u.get("email") or "").lower() == email.lower():
                self._emails[u["id"]] = u["email"]
                return u["id"]
        # Vikunja hides emails in search results but only matches a full email exactly
        if len(found) == 1 and "email" not in found[0]:
            self._emails[found[0]["id"]] = email
            return found[0]["id"]
        raise SystemExit(f"pl: no Vikunja user has the email {email}")

    def _set_assignee(self, tid, email):
        self._http("POST", f"/tasks/{tid}/assignees/bulk", {"assignees": [{"id": self._user_id(email)}]})

    def _who(self, user):
        if user.get("id") is not None and user["id"] not in self._emails:
            self._own_user()
        return self._emails.get(user.get("id")) or user.get("email") or user["username"]

    def _label_id(self, title):
        if title not in self._labels:
            found = self._http("GET", f"/labels?s={urllib.parse.quote(title)}&per_page=50") or []
            label = next((x for x in found if x["title"] == title), None)
            self._labels[title] = (label or self._http("PUT", "/labels", {"title": title}))["id"]
        return self._labels[title]

    def _set_labels(self, tid, tags):
        self._http("POST", f"/tasks/{tid}/labels/bulk",
                   {"labels": [{"id": self._label_id(t)} for t in dict.fromkeys(tags)]})

    def _card(self, task, bucket_id):
        text, meta, _ = metablock.split(task.get("description"))
        assignees = task.get("assignees") or []
        return {"id": f"vikunja#{task['id']}", "title": task["title"], "description": text, "metadata": meta,
                "tags": [x["title"] for x in task.get("labels") or []], "list_id": str(bucket_id),
                "updated_at": task["updated"], "assigned_to": self._who(assignees[0]) if assignees else None}

    def columns(self):
        if self._columns is None:
            self._columns = {b["title"]: str(b["id"]) for b in self._http("GET", f"{self._view_path()}/buckets")}
        return self._columns

    def _col(self, name):
        if name in self.columns():
            return self.columns()[name]
        if name in self.columns().values():
            return name
        raise SystemExit(f"pl: the vikunja view has no column {name!r}")

    def ensure_column(self, name, *, description=""):
        if name not in self.columns():
            made = self._http("PUT", f"{self._view_path()}/buckets", {"title": name})
            self.columns()[name] = str(made["id"])
        return self.columns()[name]

    def cards(self, query=None):
        out = []
        for bucket in self._http("GET", f"{self._view_path()}/tasks?per_page={PAGE}"):
            tasks, page = bucket.get("tasks") or [], 1
            size = last = len(tasks)   # the server may cap per_page (max_items_per_page): its first page is the page size
            while size and last == size:
                page += 1
                more = self._http("GET", f"{self._view_path()}/tasks?per_page={PAGE}&page={page}"
                                  f"&filter={urllib.parse.quote('bucket_id = %d' % bucket['id'])}") or []
                # Vikunja 2 answers a bucket filter with that bucket's tasks as a flat list; older ones group by bucket
                got = (next((b.get("tasks") or [] for b in more if b.get("id") == bucket["id"]), [])
                       if more and "tasks" in more[0] else more)
                tasks, last = tasks + got, len(got)
            out += [self._card(t, bucket["id"]) for t in tasks]
        return out

    def card(self, item_id):
        tid = self._tid(item_id)
        task = self._http("GET", f"/tasks/{tid}?expand=buckets")
        for b in task.get("buckets") or []:
            if b.get("project_view_id") == self.view:
                return self._card(task, b["id"])
        return next(c for c in self.cards() if c["id"] == f"vikunja#{tid}")

    def create(self, column, *, title, description="", tags=(), metadata=None, assigned_to=None):
        bucket = self._col(column)
        task = self._http("PUT", f"/projects/{self.project}/tasks",
                          {"title": title, "description": metablock.join(description, metadata)})
        self._move(task["id"], bucket)
        if tags:
            self._set_labels(task["id"], tags)
        if assigned_to:
            self._set_assignee(task["id"], assigned_to)
        return self.card(task["id"])

    def _move(self, tid, bucket):
        self._http("POST", f"{self._view_path()}/buckets/{bucket}/tasks", {"task_id": tid})

    def move(self, ref, column):
        self._move(self._tid(ref), self._col(column))
        return ref

    def update(self, item_id, *, verify=True, **fields):
        tid = self._tid(item_id)
        bucket = self._col(fields["column"]) if "column" in fields else fields.get("list_id")
        if {"title", "description", "metadata"} & fields.keys():
            task = self._http("GET", f"/tasks/{tid}")
            text, meta, _ = metablock.split(task.get("description"))
            if "metadata" in fields:
                meta = {**meta, **fields["metadata"]} if verify else dict(fields["metadata"])
            task["title"] = fields.get("title", task["title"])
            task["description"] = metablock.join(fields.get("description", text), meta)
            self._http("POST", f"/tasks/{tid}", task)
        if "tags" in fields:
            self._set_labels(tid, fields["tags"])
        if fields.get("assigned_to"):
            self._set_assignee(tid, fields["assigned_to"])
        if bucket is not None:
            self._move(tid, bucket)
        got = self.card(item_id)
        if verify:
            self._check(item_id, fields, got, bucket)
        return got

    @staticmethod
    def _check(item_id, fields, got, bucket):
        want = {k: fields[k] for k in ("title", "description") if k in fields}
        for k, v in want.items():
            if got[k] != v:
                raise SystemExit(f"pl: field {k} did not persist on {item_id}")
        for k, v in (fields.get("metadata") or {}).items():
            if got["metadata"].get(k) != v:
                raise SystemExit(f"pl: field metadata did not persist on {item_id}")
        if "tags" in fields and sorted(got["tags"]) != sorted(set(fields["tags"])):
            raise SystemExit(f"pl: field tags did not persist on {item_id}")
        if bucket is not None and got["list_id"] != str(bucket):
            raise SystemExit(f"pl: field list_id did not persist on {item_id}")

    def delete(self, item_id):
        self._http("DELETE", f"/tasks/{self._tid(item_id)}")

    def url(self, item_id):
        return f"{self.url_base}/tasks/{self._tid(item_id)}"

    def _check_url(self):
        try:
            reply = json.loads(self._open("GET", "/info"))
            if "version" in reply:
                return None
            reason = "not a Vikunja reply"
        except urllib.error.HTTPError as e:
            reason = e.code
        except urllib.error.URLError as e:
            reason = e.reason
        except (OSError, ValueError, TypeError):
            reason = "not a Vikunja reply"
        return f"url is wrong: {self.url_base} does not answer as Vikunja ({reason}); check [tracker] url"

    def _check_token(self):
        try:
            self._open("GET", "/user")
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return (f"token is wrong or expired: {self.url_base} refused it ({e.code}); "
                        "check the token variable, or that the token has the user permission")
            return f"token check failed: {e.code}"
        except OSError as e:
            return f"token check failed: {e}"
        return None

    def _check_view(self):
        wrong = (f"view_id is wrong: project {self.project} has no kanban view {self.view}; "
                 "check [tracker] view_id")
        try:
            kind = json.loads(self._open("GET", self._view_path())).get("view_kind")
        except urllib.error.HTTPError as e:
            return wrong if e.code in (403, 404) else f"view check failed: {e.code}"
        except (OSError, ValueError, AttributeError) as e:
            return f"view check failed: {e}"
        return None if kind in ("kanban", 3) else wrong

    def test(self):
        for check in (self._check_url, self._check_token, self._check_view):
            if problem := check():
                return util.mask(problem)
        return util.mask(f"ok: vikunja {self.url_base} project {self.project}, {len(self.columns())} columns")
