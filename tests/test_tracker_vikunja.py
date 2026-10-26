"""Vikunja connector against an in-memory fake server on loopback: no network, no real Vikunja."""
import argparse
import json
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from pl import board, trackers
from pl import config as C
from pl.trackers import metablock, vikunja

TOKEN = "tk_secret_value_123"
P, V = 7, 3


class Fake:
    def __init__(self):
        self.buckets = [{"id": 10, "title": "Inbox"}, {"id": 11, "title": "Spec ready"}, {"id": 12, "title": "Done"}]
        self.tasks = {}
        self.in_bucket = {}
        self.clock = 0
        self.next_id = 100
        self.writes = []
        self.labels = []
        self.me = {"id": 1, "username": "me", "email": "me@x"}
        self.users = [self.me, {"id": 2, "username": "ann", "email": "ann@x"},
                      {"id": 3, "username": "bob", "email": "bob@x"}]

    def writes_to(self, path):
        return [w for w in self.writes if w == ("PUT", path)]

    def stamp(self):
        self.clock += 1
        return f"2026-01-01T00:00:{self.clock:02d}Z"

    def add_task(self, bucket, **fields):
        self.next_id += 1
        task = {"id": self.next_id, "title": "t", "description": "", "priority": 0, "labels": None,
                "assignees": None, "done": False, "updated": self.stamp(), **fields}
        self.tasks[task["id"]] = task
        self.in_bucket[task["id"]] = bucket
        return task


def _handler(fake):
    base = f"/api/v1/projects/{P}/views/{V}"

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj=None):
            raw = b"" if obj is None else json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _run(self, method):
            if method == "GET" and self.path == "/api/v1/info":
                return self._send(200, {"version": "v0.24.0"})
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                return self._send(401, {"message": "missing, malformed, expired or otherwise invalid token"})
            parts = urlsplit(self.path)
            q = parse_qs(parts.query)
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n)) if n else None
            if method != "GET":
                fake.writes.append((method, parts.path))
            path = parts.path
            if method == "GET" and path == f"/api/v1/projects/{P}/views/{V}":
                return self._send(200, {"id": V, "view_kind": "kanban"})
            if method == "GET" and path == f"/api/v1/projects/{P}/views/4":
                return self._send(200, {"id": 4, "view_kind": "list"})
            if path == f"{base}/buckets":
                if method == "PUT":
                    b = {"id": 20 + len(fake.buckets), "title": body["title"]}
                    fake.buckets.append(b)
                    return self._send(201, b)
                return self._send(200, fake.buckets)
            if path == f"{base}/tasks":
                per, page = int(q.get("per_page", ["50"])[0]), int(q.get("page", ["1"])[0])
                only = re.search(r"bucket_id=(\d+)", q.get("filter", [""])[0])
                out = []
                for b in fake.buckets:
                    if only and int(only.group(1)) != b["id"]:
                        continue
                    mine = [t for i, t in fake.tasks.items() if fake.in_bucket[i] == b["id"]]
                    out.append({**b, "tasks": mine[(page - 1) * per:page * per] or None})
                return self._send(200, out)
            if m := re.fullmatch(rf"{base}/buckets/(\d+)/tasks", path):
                fake.in_bucket[body["task_id"]] = int(m.group(1))
                fake.tasks[body["task_id"]]["updated"] = fake.stamp()
                return self._send(200, {})
            if path == f"/api/v1/projects/{P}/tasks" and method == "PUT":
                t = fake.add_task(10, title=body["title"], description=body.get("description", ""))
                return self._send(201, t)
            if path == "/api/v1/user":
                return self._send(200, fake.me)
            if path == "/api/v1/users":
                s = q.get("s", [""])[0].lower()
                return self._send(200, [{k: u[k] for k in ("id", "username")} for u in fake.users if u["email"] == s])
            if path == "/api/v1/labels":
                if method == "PUT":
                    label = {"id": 50 + len(fake.labels), "title": body["title"]}
                    fake.labels.append(label)
                    return self._send(201, label)
                s = q.get("s", [""])[0]
                return self._send(200, [x for x in fake.labels if s in x["title"]])
            if m := re.fullmatch(r"/api/v1/tasks/(\d+)/(labels|assignees)/bulk", path):
                task = fake.tasks[int(m.group(1))]
                if m.group(2) == "labels":
                    task["labels"] = [x for i in body["labels"] for x in fake.labels if x["id"] == i["id"]]
                else:
                    task["assignees"] = [{k: u[k] for k in ("id", "username")}
                                         for i in body["assignees"] for u in fake.users if u["id"] == i["id"]]
                task["updated"] = fake.stamp()
                return self._send(200, {})
            if m := re.fullmatch(r"/api/v1/tasks/(\d+)", path):
                tid = int(m.group(1))
                if tid not in fake.tasks:
                    return self._send(404, {"message": "not found"})
                if method == "GET":
                    t = dict(fake.tasks[tid])
                    if q.get("expand") == ["buckets"]:
                        t["buckets"] = [{"id": fake.in_bucket[tid], "project_view_id": V}]
                    return self._send(200, t)
                if method == "POST":
                    fake.tasks[tid] = {**body, "id": tid, "updated": fake.stamp()}
                    return self._send(200, fake.tasks[tid])
                if method == "DELETE":
                    del fake.tasks[tid]
                    return self._send(200, {"message": "ok"})
            return self._send(404, {"message": "no route"})

        do_GET = lambda self: self._run("GET")      # noqa: E731
        do_PUT = lambda self: self._run("PUT")      # noqa: E731
        do_POST = lambda self: self._run("POST")    # noqa: E731
        do_DELETE = lambda self: self._run("DELETE")  # noqa: E731

    return H


@pytest.fixture
def fake(monkeypatch):
    f = Fake()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(f))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("VIKUNJA_TOKEN", TOKEN)
    monkeypatch.setattr(C, "TRACKER", {"type": "vikunja", "url": f"http://127.0.0.1:{server.server_port}/",
                                       "token": "${VIKUNJA_TOKEN}", "project_id": P, "view_id": V})
    trackers.reset()
    f.tracker = vikunja.Vikunja(C.TRACKER)
    yield f
    server.shutdown()
    server.server_close()
    trackers.reset()
    trackers.set_dry_run(False)


def test_columns_are_the_view_buckets(fake):
    assert fake.tracker.columns() == {"Inbox": "10", "Spec ready": "11", "Done": "12"}


def test_cards_list_tasks_by_bucket_and_page(fake, monkeypatch):
    monkeypatch.setattr(vikunja, "PAGE", 2)
    for i in range(5):
        fake.add_task(10, title=f"a{i}")
    fake.add_task(11, title="b", labels=[{"title": "bug"}], assignees=[{"username": "ann"}])
    cards = fake.tracker.cards()
    assert sorted(c["title"] for c in cards) == ["a0", "a1", "a2", "a3", "a4", "b"]
    b = next(c for c in cards if c["title"] == "b")
    assert b["id"] == f"vikunja#{b['id'].split('#')[1]}" and b["list_id"] == "11"
    assert b["tags"] == ["bug"] and b["assigned_to"] == "ann"
    assert {"id", "title", "description", "tags", "metadata", "list_id", "updated_at", "assigned_to"} <= set(b)
    assert next(c for c in cards if c["title"] == "a0")["assigned_to"] is None


def test_create_puts_task_in_column_and_reads_back(fake):
    c = fake.tracker.create("Spec ready", title="T", description="text", metadata={"k": 1}, tags=["x"], assigned_to="me@x")
    assert c["list_id"] == "11" and c["title"] == "T"
    assert c["description"] == "text" and c["metadata"] == {"k": 1}
    assert fake.in_bucket[int(c["id"].split("#")[1])] == 11


def test_ensure_column_creates_missing_inbox(fake):
    assert fake.tracker.ensure_column("Inbox") == "10"
    new = fake.tracker.ensure_column("Review")
    assert new == str(fake.buckets[-1]["id"]) and fake.tracker.columns()["Review"] == new


def test_update_list_id_moves_task(fake):
    c = fake.tracker.create("Inbox", title="T")
    r = fake.tracker.update(c["id"], list_id="12")
    assert r["list_id"] == "12" and fake.in_bucket[int(c["id"].split("#")[1])] == 12
    assert fake.tracker.update(c["id"], column="Spec ready")["list_id"] == "11"


def _rich(fake):
    return fake.add_task(10, title="old", description=metablock.join("body", {"a": 1}), priority=4,
                         labels=[{"id": 1, "title": "bug"}], assignees=[{"id": 9, "username": "ann"}])


def test_update_title_keeps_assignee_description_labels_priority(fake):
    t = _rich(fake)
    fake.tracker.update(f"vikunja#{t['id']}", title="new")
    got = fake.tasks[t["id"]]
    assert got["title"] == "new" and got["priority"] == 4
    assert got["assignees"] == t["assignees"] and got["labels"] == t["labels"]
    assert not [w for w in fake.writes if w[1].endswith("/bulk")]
    assert metablock.split(got["description"])[:2] == ("body", {"a": 1})


def test_update_metadata_only_keeps_fields(fake):
    t = _rich(fake)
    fake.tracker.update(f"vikunja#{t['id']}", metadata={"b": 2})
    got = fake.tasks[t["id"]]
    assert got["title"] == "old" and got["priority"] == 4 and got["assignees"] == t["assignees"]
    assert metablock.split(got["description"])[:2] == ("body", {"a": 1, "b": 2})


def test_description_keeps_metablock_and_text_readable(fake):
    c = fake.tracker.create("Inbox", title="T", description="hello", metadata={"k": "v"})
    raw = fake.tasks[int(c["id"].split("#")[1])]["description"]
    assert raw.startswith("hello\n\n") and metablock.MARK in raw
    c = fake.tracker.update(c["id"], description="changed")
    assert c["description"] == "changed" and c["metadata"] == {"k": "v"}


def test_updated_at_follows_task_updated(fake):
    c = fake.tracker.create("Inbox", title="T")
    before = fake.tracker.card(c["id"])["updated_at"]
    fake.tracker.update(c["id"], title="U")
    assert fake.tracker.card(c["id"])["updated_at"] > before


def test_delete_removes_task(fake):
    c = fake.tracker.create("Inbox", title="T")
    fake.tracker.delete(c["id"])
    assert fake.tasks == {}


def test_url_points_at_task_page(fake):
    assert fake.tracker.url("vikunja#12") == f"{C.TRACKER['url'].rstrip('/')}/tasks/12"


def test_bad_token_error_is_masked(fake, monkeypatch):
    monkeypatch.setenv("VIKUNJA_TOKEN", "wrong_token_value_999")
    with pytest.raises(SystemExit) as e:
        vikunja.Vikunja(C.TRACKER).columns()
    assert "401" in str(e.value) and "wrong_token_value_999" not in str(e.value)


def test_test_reports_columns(fake):
    assert fake.tracker.test() == f"ok: vikunja {fake.tracker.url_base} project {P}, 3 columns"


def _tester(fake, **over):
    return vikunja.Vikunja({**C.TRACKER, **over})


def _closed_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_test_names_wrong_url_when_info_fails(fake):
    for url in (f"{fake.tracker.url_base}/nope", f"http://127.0.0.1:{_closed_port()}"):
        line = _tester(fake, url=url).test()
        assert not line.startswith("ok") and "url is wrong" in line and url in line


def test_test_names_wrong_url_for_non_vikunja_reply(fake):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            raw = b"<html>hello</html>"
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        line = _tester(fake, url=f"http://127.0.0.1:{server.server_port}").test()
    finally:
        server.shutdown()
        server.server_close()
    assert "url is wrong" in line and "not a Vikunja reply" in line


def test_test_names_wrong_token_without_showing_it(fake, monkeypatch):
    monkeypatch.setenv("VIKUNJA_TOKEN", "wrong_token_value_999")
    line = fake.tracker.test()
    assert "token is wrong or expired" in line and "wrong_token_value_999" not in line
    assert "(401)" in line or "(403)" in line
    assert "permission" in line


def test_test_names_wrong_view_id(fake):
    for view in (99, 4):
        line = _tester(fake, view_id=view).test()
        assert "view_id is wrong" in line and f"project {P}" in line and str(view) in line


def test_test_reports_first_failing_check(fake, monkeypatch):
    monkeypatch.setenv("VIKUNJA_TOKEN", "wrong_token_value_999")
    assert "token is wrong" in _tester(fake, view_id=99).test()
    assert "url is wrong" in _tester(fake, url=f"http://127.0.0.1:{_closed_port()}").test()


@pytest.mark.parametrize("case", ["ok", "url", "token", "view"])
def test_no_output_or_file_holds_the_token(fake, monkeypatch, tmp_path, capsys, case):
    from pl import doctor, local_model
    secret = TOKEN
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(local_model, "_request", lambda *a: json.dumps({"models": []}))
    monkeypatch.setattr(C, "INTAKE", {})
    if case == "url":
        monkeypatch.setitem(C.TRACKER, "url", f"http://127.0.0.1:{_closed_port()}")
    elif case == "token":
        secret = "wrong_token_value_999"
        monkeypatch.setenv("VIKUNJA_TOKEN", secret)
    elif case == "view":
        monkeypatch.setitem(C.TRACKER, "view_id", 99)
    trackers.reset()
    with pytest.raises(SystemExit):
        doctor.cmd_doctor(argparse.Namespace(json=False))
    out, err = capsys.readouterr()
    assert secret not in out + err
    assert not any(secret in f.read_text(errors="ignore") for f in tmp_path.rglob("*") if f.is_file())


def test_dry_run_sends_no_write(fake):
    t = _rich(fake)
    trackers.set_dry_run()
    tr = trackers.get("tracker")
    tr.create("Inbox", title="x")
    tr.update(f"vikunja#{t['id']}", title="y")
    tr.delete(f"vikunja#{t['id']}")
    assert fake.writes == []
    tr.create("Inbox", title="x", tags=["a"], assigned_to="me@x")
    tr.update(f"vikunja#{t['id']}", tags=["b"], assigned_to="ann@x")
    assert fake.writes == []


def test_board_commands_create_move_update(fake):
    tr = trackers.get("tracker")
    c = tr.create("Inbox", title="T", description="d")
    assert board.matches(c["id"], tr.cards()) and board.matches("#" + c["id"].split("#")[1], tr.cards())
    board.update(c["id"], list_id=tr.columns()["Spec ready"])
    board.update(c["id"], title="T2")
    got = tr.card(c["id"])
    assert got["list_id"] == "11" and got["title"] == "T2" and got["description"] == "d"


def _titles(task):
    return sorted(x["title"] for x in task["labels"])


def test_create_sets_tags_as_labels(fake):
    c = fake.tracker.create("Inbox", title="T", tags=["idea", "from-pl-idea"])
    assert _titles(fake.tasks[int(c["id"].split("#")[1])]) == ["from-pl-idea", "idea"]
    assert sorted(c["tags"]) == ["from-pl-idea", "idea"]


def test_missing_label_created_once_and_reused(fake):
    fake.labels.append({"id": 40, "title": "xy"})
    fake.tracker.create("Inbox", title="A", tags=["x"])
    fake.tracker.create("Inbox", title="B", tags=["x"])
    assert sorted(x["title"] for x in fake.labels) == ["x", "xy"]
    assert len(fake.writes_to("/api/v1/labels")) == 1
    assert fake.tracker.cards()[0]["tags"] == ["x"]


def test_update_tags_replaces_labels_keeps_label_records(fake):
    c = fake.tracker.create("Inbox", title="T", tags=["a", "b"])
    got = fake.tracker.update(c["id"], title="T2", tags=["b", "c"])
    assert sorted(got["tags"]) == ["b", "c"] and got["title"] == "T2"
    assert "a" in [x["title"] for x in fake.labels]


def test_create_assigns_me_by_email(fake):
    c = fake.tracker.create("Inbox", title="T", assigned_to="ME@x")
    assert [a["id"] for a in fake.tasks[int(c["id"].split("#")[1])]["assignees"]] == [1]
    assert c["assigned_to"] == "me@x"


def test_update_assigns_other_user_by_email_and_replaces(fake):
    t = fake.add_task(10, assignees=[{"id": 2, "username": "ann"}])
    got = fake.tracker.update(f"vikunja#{t['id']}", assigned_to="bob@x")
    assert [a["id"] for a in fake.tasks[t["id"]]["assignees"]] == [3]
    assert got["assigned_to"] == "bob@x"


def test_unknown_email_fails_plainly_task_kept(fake):
    with pytest.raises(SystemExit) as e:
        fake.tracker.create("Inbox", title="T", assigned_to="nobody@x")
    assert "nobody@x" in str(e.value) and "no Vikunja user" in str(e.value) and "TOKEN" not in str(e.value)
    assert [t["assignees"] for t in fake.tasks.values()] == [None]


def test_unmapped_assignee_reads_username(fake):
    t = fake.add_task(10, assignees=[{"id": 2, "username": "ann"}])
    assert fake.tracker.card(f"vikunja#{t['id']}")["assigned_to"] == "ann"
