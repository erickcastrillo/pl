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

    @staticmethod
    def _card(task, bucket_id):
        text, meta, _ = metablock.split(task.get("description"))
        assignees = task.get("assignees") or []
        return {"id": f"vikunja#{task['id']}", "title": task["title"], "description": text, "metadata": meta,
                "tags": [x["title"] for x in task.get("labels") or []], "list_id": str(bucket_id),
                "updated_at": task["updated"], "assigned_to": assignees[0]["username"] if assignees else None}

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
            while len(tasks) == PAGE * page:
                page += 1
                more = self._http("GET", f"{self._view_path()}/tasks?per_page={PAGE}&page={page}"
                                  f"&filter={urllib.parse.quote('bucket_id=%d' % bucket['id'])}")
                tasks += next((b.get("tasks") or [] for b in more if b["id"] == bucket["id"]), [])
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
                return f"token is wrong or expired: {self.url_base} refused it ({e.code}); check the token variable"
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
