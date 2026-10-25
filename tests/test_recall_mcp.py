"""The memory MCP backend: a connector with role "memory" serves the entries, and pl serves memory_search."""
import json
import types

import pytest

from pl import config as C
from pl import connectors, mcp_serve, recall
from pl.trackers import mcp as M


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PL_CONFIG_DIR", raising=False)
    d = tmp_path / ".pl-t"
    d.mkdir()
    (d / "config.toml").write_text("")
    C.load("t")
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


class FakeSession:
    def __init__(self, replies):
        self.replies = replies
        self.calls = []

    def run(self, fn):
        return fn(self)

    def call_tool(self, tool, args):
        self.calls.append((tool, args))
        text = json.dumps(self.replies[tool])
        return types.SimpleNamespace(isError=False, content=[types.SimpleNamespace(type="text", text=text)])


@pytest.fixture
def hub(monkeypatch):
    session = FakeSession({})
    monkeypatch.setattr(connectors, "load", lambda: {"hub": {"role": ["memory"], "enabled": True, "url": "http://hub/mcp"}})
    monkeypatch.setattr(M, "_session", lambda server, timeout, as_is=False: ("k", session))
    return session


def test_cli_search_with_the_mcp_backend_returns_the_servers_results_unchanged(hub):
    hits = [{"id": "m-1", "kind": "decision", "text": "split into 3 PRs", "source": "card:42"}]
    hub.replies["memory_search"] = hits
    assert recall.search("split into 3 PRs", limit=5, pod="eng", card="42") == hits
    assert hub.calls == [("memory_search", {"query": "split into 3 PRs", "limit": 5, "pod": "eng", "card": "42"})]


def test_get_and_forget_contact_go_to_the_server(hub):
    hub.replies["memory_get"] = {"id": "m-1", "text": "x"}
    hub.replies["memory_forget"] = {"deleted": 3}
    assert recall.get("m-1") == {"id": "m-1", "text": "x"}
    assert recall.forget_contact("c-9") == 3
    assert [c[0] for c in hub.calls] == ["memory_get", "memory_forget"]


def test_the_secret_check_runs_before_an_mcp_add(hub, monkeypatch):
    monkeypatch.setattr(recall.util, "mask", lambda t: t.replace("hunter2", "***"))
    with pytest.raises(recall.Refused):
        recall.add("the password is hunter2", "fact", "test")
    assert hub.calls == []


def test_an_mcp_add_sends_the_cleaned_entry(hub):
    hub.replies["memory_add"] = {"id": "m-7"}
    assert recall.add("card 4111 1111 1111 1111 paid", "fact", "test", card="42") == "m-7"
    tool, args = hub.calls[0]
    assert tool == "memory_add" and "****1111" in args["text"] and args["card"] == "42"


def test_without_a_memory_connector_the_local_store_answers(monkeypatch):
    monkeypatch.setattr(connectors, "load", lambda: {})
    assert recall.backend() is None
    assert recall.forget_contact("nobody") == 0


def test_mcp_memory_search_equals_the_cli_json(monkeypatch):
    hits = [{"id": "m-1", "text": "split into 3 PRs"}]
    monkeypatch.setattr(recall, "search", lambda q, limit=10, pod=None, card=None: hits)
    assert mcp_serve.memory_search("split") == hits
