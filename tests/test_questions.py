import copy
import hashlib
import json
import types
import pytest

from pl import board, commands, config as C, dispatch, trackers, util
from pl import questions


PANE_W = {"stage": "spec", "session_id": "s1", "pane": "%5", "window": "@7", "profile": "acme",
          "started_at": "2026-09-30T10:00:00+00:00"}


class FakeTracker:
    def __init__(self, items):
        self.items = {c["id"]: c for c in items}
        self.writes = []

    def columns(self):
        return {t: f"col-{t}" for t in C.COLUMNS}

    def cards(self, query=None):
        return [copy.deepcopy(c) for c in self.items.values()]

    def card(self, item_id):
        return copy.deepcopy(self.items[item_id])

    def update(self, item_id, *, verify=True, **fields):
        self.writes.append((item_id, copy.deepcopy(fields)))
        c = self.items[item_id]
        if "column" in fields:
            fields["list_id"] = self.columns()[fields.pop("column")]
        if "metadata" in fields:
            c["metadata"] = {**(c.get("metadata") or {}), **fields.pop("metadata")}
        c.update(fields)
        return copy.deepcopy(c)

    def url(self, item_id):
        return f"https://example.test/{item_id}"


def mk(cid, col="Inbox", desc="", **meta):
    return {"id": cid, "title": f"Card {cid[:8]}", "description": desc, "tags": [], "list_id": f"col-{col}",
            "updated_at": "2026-10-01T12:00:00+00:00", "metadata": {"pipeline_mode": "auto", "profile": "acme", **meta}}


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("PL_CONFIG_DIR", "PL_TMUX_SESSION"):
        monkeypatch.delenv(var, raising=False)
    d = tmp_path / ".pl-t"
    d.mkdir()
    (d / "config.toml").write_text('[code_host.labels]\nreview = "pl:auto-review"\nready = "pl:ready"\n')
    C.load("t")
    C.USER_EMAIL = "me@example.test"
    C.PROFILES = {"acme": tmp_path / ".claude"}
    C.ACCOUNTS = {"acme": {"harness": "claude"}}
    C.PROMPTS = {**C.PROMPTS, "spec": "/pl-spec {id}"}
    monkeypatch.setattr(commands, "registry", lambda: {})
    monkeypatch.setattr(commands, "intake_configured", lambda: False)
    monkeypatch.setattr(dispatch.accounts, "exhausted_profiles", lambda *a, **k: set())
    monkeypatch.setattr(dispatch, "healthy_profile", lambda *a, **k: "acme")
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


def test_a_question_shows_in_inbox_json_and_is_sent_to_chat(monkeypatch, capsys):
    c = mk("c1", "Inbox", desc="# PIPELINE: INPUT\nBuild login\n", worker=dict(PANE_W))
    tr = FakeTracker([c])
    monkeypatch.setattr(trackers, "get", lambda name: tr)

    sent = []
    monkeypatch.setattr(questions, "_send_chat", lambda card, q, stage=None: sent.append((card["id"], q, stage)))

    args = types.SimpleNamespace(id="c1", text="Which role may edit it?", json=False)
    questions.cmd_question(args)

    updated = tr.card("c1")
    secs = board.sections(updated["description"])
    assert "QUESTIONS" in secs
    assert "Which role may edit it?" in secs["QUESTIONS"]

    qm = updated["metadata"].get("question")
    assert qm is not None
    assert qm["stage"] == "spec"
    assert qm["text_sha"] == hashlib.sha256("Which role may edit it?".encode()).hexdigest()
    assert qm.get("asked_at")

    # The worker was ended
    assert updated["metadata"].get("worker") is None
    assert any(w.get("window") == "@7" for w in (updated["metadata"].get("finished_workers") or []))

    # Chat seam received the message
    assert len(sent) == 1
    assert sent[0] == ("c1", "Which role may edit it?", "spec")

    # Inbox listing has kind "question"
    items = questions.inbox_items([updated])
    assert len(items) == 1
    it = items[0]
    assert it["kind"] == "question"
    assert it["card"] == "c1"
    assert it["stage"] == "spec"
    assert "Which role may edit it?" in it["question"]
    assert "answer" in it["command"]

    # Test --json flag
    capsys.readouterr()
    args_json = types.SimpleNamespace(id="c1", text="Another question?", json=True)
    questions.cmd_question(args_json)
    out = json.loads(capsys.readouterr().out)
    assert out["card"] == "c1"
    assert out["question"] == "Another question?"


def test_answer_writes_to_the_card_and_the_stage_restarts(monkeypatch, capsys):
    c = mk("c1", "Inbox", desc="# PIPELINE: INPUT\nBuild login\n# PIPELINE: QUESTIONS\nQ: Which role may edit it?\n")
    c["metadata"]["question"] = {
        "asked_at": util.now_iso(),
        "stage": "spec",
        "text_sha": hashlib.sha256("Which role may edit it?".encode()).hexdigest(),
    }
    tr = FakeTracker([c])
    monkeypatch.setattr(trackers, "get", lambda name: tr)

    monkeypatch.setattr(dispatch, "sweep_untracked", lambda *a, **k: 0)
    for name in ("mirror_to_product", "ensure_services"):
        monkeypatch.setattr(dispatch, name, lambda *a, **k: None)

    # While question is open, dispatcher does not start worker
    started = []
    monkeypatch.setattr(dispatch, "start_worker", lambda card, stage, attempts, dry: started.append((card["id"], stage)))
    dispatch.dispatch_once(1, False, pull=False)
    assert started == []

    # Owner answers the question
    args = types.SimpleNamespace(id="c1", text="Only administrators.", json=False)
    questions.cmd_answer(args)

    updated = tr.card("c1")
    secs = board.sections(updated["description"])
    assert "Q: Which role may edit it?" in secs["QUESTIONS"]
    assert "A: Only administrators." in secs["QUESTIONS"]
    assert updated["metadata"].get("question") is None
    assert updated["metadata"].get("answered") == 1

    # Next dispatcher pass starts the worker with answered=1 in prompt
    prompts_started = []
    def fake_start_worker(card, stage, attempts, dry):
        h, prof = dispatch.harnesses.harness_for(stage, card)
        p = C.PROMPTS[stage].format(id=card["id"], worktrees_dir="") + questions.prompt_arg(card)
        prompts_started.append(p)
        dispatch.update(card["id"], metadata={"worker": {"stage": stage}, "answered": None})

    monkeypatch.setattr(dispatch, "start_worker", fake_start_worker)
    monkeypatch.setattr(dispatch.harnesses, "harness_for", lambda stage, card: (types.SimpleNamespace(name="claude"), "acme"))
    dispatch.dispatch_once(1, False, pull=False)

    assert len(prompts_started) == 1
    assert "answered=1" in prompts_started[0]
    # answered flag is cleared after start
    assert tr.card("c1")["metadata"].get("answered") is None


def test_answer_without_an_open_question_exits_2(monkeypatch):
    c = mk("c1", "Inbox", desc="# PIPELINE: INPUT\nBuild login\n")
    tr = FakeTracker([c])
    monkeypatch.setattr(trackers, "get", lambda name: tr)

    args = types.SimpleNamespace(id="c1", text="An unprompted answer", json=False)
    with pytest.raises(SystemExit) as exc_info:
        questions.cmd_answer(args)

    assert exc_info.value.code == 2
