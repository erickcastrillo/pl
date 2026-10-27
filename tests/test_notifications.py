"""Plan 51 slice 1: notifications for PR opened, merged and done, filtered by [notify] events."""
import json

import pytest
import tomlkit

from pl import config as C
from pl import notifications
from pl.manager import machine_dir

NINE = ["plan_ready", "approved", "pr_opened", "review_passed", "review_rework", "merged", "done", "alert_opened",
        "agent_waiting"]


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("PL_CONFIG_DIR", "PL_TMUX_SESSION"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("PL_MACHINE_DIR", str(tmp_path / "machine"))
    d = tmp_path / ".pl-t"
    d.mkdir()
    (d / "config.toml").write_text("")
    C.load("t")
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


@pytest.fixture
def desktop(monkeypatch):
    calls = []
    monkeypatch.setattr(notifications, "notify", lambda title, message: calls.append((title, message)))
    return calls


class FakeAdapter:
    def __init__(self, name):
        self.name, self.sent = name, []

    def send(self, chat, text, buttons=None):
        self.sent.append((str(chat), text))


def _connectors(**routes):
    doc = tomlkit.document()
    conns = doc.setdefault("connectors", tomlkit.table())
    for name, r in routes.items():
        t = tomlkit.table()
        t.update({"role": "messaging", "adapter": "telegram", "token": "${TG}", "allow": ["1"], "routes": r})
        conns[name] = t
    machine_dir().mkdir(parents=True, exist_ok=True)
    (machine_dir() / "machine.toml").write_text(tomlkit.dumps(doc))


def test_default_events_without_a_notify_table():
    assert C.NOTIFY["events"] == NINE


def test_send_calls_attention_once_and_names_the_card(desktop):
    assert notifications.send("pr_opened", "PR opened: card0001 #77", "Add a thing") is True
    assert desktop == [("PR opened: card0001 #77", "Add a thing")]


def test_send_reaches_only_connectors_routed_for_alerts(desktop, monkeypatch):
    from pl import chat
    from pl.chat import gates
    _connectors(slack=["alerts"], tg=["gates"])
    fakes = {n: FakeAdapter(n) for n in ("slack", "tg")}
    monkeypatch.setattr(chat, "adapter", lambda n: fakes[n])
    notifications.send("merged", "Merged: card0001 #77", "Add a thing")
    gates.post_outbox()
    assert fakes["slack"].sent == [("1", "Merged: card0001 #77: Add a thing")]
    assert fakes["tg"].sent == []


def test_event_left_out_sends_nothing(desktop):
    C.NOTIFY = {"events": ["merged"]}
    assert notifications.send("pr_opened", "PR opened: card0001 #77", "x") is False
    assert desktop == []
    assert not (machine_dir() / "chat-outbox.jsonl").exists()


def test_send_in_dry_run_sends_nothing(desktop, monkeypatch):
    monkeypatch.setattr(notifications.trackers, "dry_run", lambda: True)
    assert notifications.send("merged", "t", "m") is False
    assert desktop == []


def test_a_broken_outbox_never_raises(desktop, monkeypatch):
    monkeypatch.setattr(notifications.manager, "machine_dir", lambda: (_ for _ in ()).throw(OSError("full")))
    assert notifications.send("merged", "t", "m") is True


def test_bad_events_value_is_a_config_error():
    errs = C.validate(tomlkit.parse('[notify]\nevents = ["merged", "nope"]\n'))
    assert any("notify.events" in e for e in errs)
    assert not any("notify.events" in e for e in C.validate(tomlkit.parse('[notify]\nevents = ["merged"]\n')))
    assert any("notify.events" in e for e in C.validate(tomlkit.parse('[notify]\nevents = "merged"\n')))


def test_outbox_line_is_private_json(desktop):
    notifications.send("done", "Done: card0001", "x")
    path = machine_dir() / "chat-outbox.jsonl"
    assert json.loads(path.read_text()) == {"key": "notify", "headline": "Done: card0001: x", "route": "alerts"}
    assert path.stat().st_mode & 0o777 == 0o600


# --- the dispatcher's move check ---

import argparse  # noqa: E402

from pl import commands, dispatch  # noqa: E402


def _card(n, col, **meta):
    return {"id": f"card000{n}", "title": f"Title {n}", "description": "", "list_id": col,
            "metadata": {"pipeline_mode": "auto", **meta}}


class Board:
    """Cards whose column is their list_id; a pass over them runs nothing real."""

    def __init__(self, monkeypatch, *cards):
        self.cards = list(cards)
        monkeypatch.setattr(dispatch, "registry", lambda: {})
        monkeypatch.setattr(dispatch, "cards", lambda: self.cards)
        monkeypatch.setattr(dispatch, "col_name", lambda lid: lid)
        monkeypatch.setattr(dispatch, "stage_for", lambda c, col: None)
        monkeypatch.setattr(dispatch, "mirror_to_product", lambda *a, **k: None)
        monkeypatch.setattr(dispatch, "ensure_services", lambda *a, **k: None)
        monkeypatch.setattr(dispatch, "tmux", lambda *a, **k: "")
        monkeypatch.setattr(dispatch, "sweep_untracked", lambda *a, **k: None)

    def move(self, n, col):
        self.cards[n - 1]["list_id"] = col

    def run(self):
        dispatch.dispatch_once(1, False, pull=False)


def test_pr_open_move_sends_one_pr_opened_with_pr_number(desktop, monkeypatch):
    b = Board(monkeypatch, _card(1, "In progress", pr_urls=["https://github.com/o/r/pull/77"]))
    b.run()
    b.move(1, "PR open")
    b.run()
    b.run()
    assert len(desktop) == 1
    assert "card0001" in desktop[0][0] and "#77" in desktop[0][0]


def test_one_merge_sends_one_merged_and_no_done(desktop, monkeypatch):
    b = Board(monkeypatch, _card(1, "PR open", pr_urls=["https://github.com/o/r/pull/77"]))
    b.run()
    b.move(1, "Done")
    b.run()
    assert [t for t, _ in desktop] == ["Merged: card0001 #77"]


def test_three_merges_in_one_pass_send_one_notification(desktop, monkeypatch):
    b = Board(monkeypatch, *[_card(n, "PR open", pr_urls=[f"https://github.com/o/r/pull/{70 + n}"]) for n in (1, 2, 3)])
    b.run()
    for n in (1, 2, 3):
        b.move(n, "Done")
    b.run()
    assert len(desktop) == 1
    title, message = desktop[0]
    assert title == "Merged: 3 PRs"
    assert all(f"#{70 + n}" in message for n in (1, 2, 3))


def test_pl_done_move_is_seen_once(desktop, monkeypatch):
    b = Board(monkeypatch, _card(1, "PR open", pr_urls=["https://github.com/o/r/pull/77"]))
    b.run()
    monkeypatch.setattr(commands, "card", lambda cid: b.cards[0])
    monkeypatch.setattr(commands, "col_id", lambda name: name)
    monkeypatch.setattr(commands, "col_name", lambda lid: lid)
    monkeypatch.setattr(commands, "update", lambda cid, list_id=None, **k: b.move(1, list_id))
    monkeypatch.setattr(commands, "pick", lambda ref, cs, what: b.cards[0])
    commands.done("card0001", cs=b.cards)
    assert desktop == []          # pl done only moves the card
    b.run()
    b.run()
    assert [t for t, _ in desktop] == ["Merged: card0001 #77"]


def test_done_without_pr_sends_done(desktop, monkeypatch):
    b = Board(monkeypatch, _card(1, "Approved"))
    b.run()
    b.move(1, "Done")
    b.run()
    assert [t for t, _ in desktop] == ["Done: card0001"]


def test_dropped_card_sends_nothing(desktop, monkeypatch):
    b = Board(monkeypatch, _card(1, "Approved", dropped_at="2026-10-26T00:00:00+00:00"))
    b.run()
    b.move(1, "Done")
    b.run()
    assert desktop == []


def test_a_failing_notifier_does_not_stop_the_pass(monkeypatch, capsys):
    def boom(title, message):
        raise OSError("no such command")
    monkeypatch.setattr(notifications, "notify", boom)
    b = Board(monkeypatch, _card(1, "Approved"))
    b.run()
    b.move(1, "Done")
    b.run()
    assert "notification" in capsys.readouterr().err
    assert json.loads(C.STATE_FILE.read_text())["last_col"] == {"card0001": "Done"}


def test_plan_ready_names_the_card_and_obeys_the_event_list(desktop, monkeypatch):
    b = Board(monkeypatch, _card(1, "Plan for review"))
    b.run()
    assert desktop and desktop[0][0].startswith("Plan ready: card0001")
    desktop.clear()
    C.NOTIFY = {"events": ["merged"]}
    b = Board(monkeypatch, _card(2, "Plan for review"))
    b.run()
    assert desktop == []


def test_approved_obeys_the_event_list(desktop, monkeypatch):
    c = _card(1, "Plan for review")
    for name, val in (("fresh_next", lambda: None), ("find_card", lambda ref: c), ("col_name", lambda lid: lid),
                      ("update", lambda *a, **k: None), ("col_id", lambda n: n), ("edited_plan", lambda *a: None),
                      ("plan_path", lambda c: None)):
        monkeypatch.setattr(commands, name, val)
    monkeypatch.setattr(commands.pods, "learn", lambda *a: None)
    monkeypatch.setattr(commands.recall, "note", lambda *a: None)
    monkeypatch.setattr(commands.audit, "append", lambda *a, **k: None)
    monkeypatch.setattr(commands.events, "emit", lambda *a, **k: None)
    monkeypatch.setattr(commands.confirm, "agent_session", lambda: False)
    monkeypatch.setattr(commands.pipeline, "is_config", lambda: False)
    a = argparse.Namespace(id="card0001", force=False)
    commands.cmd_approve(a)
    assert desktop and desktop[0][0].startswith("Approved: card0001")
    desktop.clear()
    C.NOTIFY = {"events": ["merged"]}
    commands.cmd_approve(a)
    assert desktop == []


# --- the review result ---

from pl import rework  # noqa: E402
from pl.util import load_state, save_state  # noqa: E402

P7, P8 = "https://github.com/o/r/pull/7", "https://github.com/o/r/pull/8"


@pytest.fixture
def labeled(monkeypatch):
    C.CODE_HOST = {**C.CODE_HOST, "labels": {"ready": "pl:ready", "rework": "pl:rw", "failed": "pl:bad"}}
    rework.SEEN.clear()
    yield rework.SEEN
    rework.SEEN.clear()


def _cols():
    return {"card0001": "PR open"}


def _pr_card(col="PR open", *urls):
    return _card(1, col, pr_urls=list(urls or [P7]))


def test_passed_label_sends_one_review_passed_naming_card_and_pr(desktop, labeled):
    labeled[P7] = frozenset({"pl:ready"})
    notifications.review_passed([_pr_card()], _cols(), {})
    assert len(desktop) == 1
    assert "card0001" in desktop[0][0] and "#7" in desktop[0][0]


def test_second_pass_and_restart_send_nothing(desktop, labeled):
    labeled[P7] = frozenset({"pl:ready"})
    st = {"notified": {}}
    notifications.review_passed([_pr_card()], _cols(), st)
    notifications.review_passed([_pr_card()], _cols(), st)
    save_state(st)
    notifications.review_passed([_pr_card()], _cols(), load_state())
    assert len(desktop) == 1


def test_two_prs_passing_in_one_pass_send_one_listing_both(desktop, labeled):
    labeled[P7] = labeled[P8] = frozenset({"pl:ready"})
    st = {}
    notifications.review_passed([_pr_card("PR open", P7, P8)], _cols(), st)
    assert len(desktop) == 1 and "#7" in desktop[0][0] and "#8" in desktop[0][0]
    notifications.review_passed([_pr_card("PR open", P7, P8)], _cols(), st)
    assert len(desktop) == 1


@pytest.mark.parametrize("other", ["pl:rw", "pl:bad"])
def test_ready_with_rework_or_failed_is_not_a_pass(desktop, labeled, other):
    labeled[P7] = frozenset({"pl:ready", other})
    notifications.review_passed([_pr_card()], _cols(), {})
    assert desktop == []


def test_review_events_switched_off_send_nothing(desktop, labeled):
    labeled[P7] = frozenset({"pl:ready"})
    C.NOTIFY = {"events": ["review_rework"]}
    st = {}
    notifications.review_passed([_pr_card()], _cols(), st)
    assert desktop == [] and not (machine_dir() / "chat-outbox.jsonl").exists()
    assert f"review_passed:{P7}" in st["notified"]
    C.NOTIFY = {"events": ["review_passed"]}
    notifications.review_passed([_pr_card()], _cols(), st)
    assert desktop == []
    labeled[P8] = frozenset({"pl:ready"})
    notifications.review_passed([_pr_card("PR open", P8)], _cols(), st)
    assert len(desktop) == 1


def test_without_a_ready_label_nothing_is_sent(desktop, labeled):
    C.CODE_HOST = {**C.CODE_HOST, "labels": {}}
    labeled[P7] = frozenset({"pl:ready"})
    notifications.review_passed([_pr_card()], _cols(), {})
    assert desktop == []


def test_keys_of_done_cards_are_pruned(desktop, labeled):
    labeled[P7] = frozenset({"pl:ready"})
    st = {}
    notifications.review_passed([_pr_card()], _cols(), st)
    assert f"review_passed:{P7}" in st["notified"]
    notifications.review_passed([_pr_card("Done")], {"card0001": "Done"}, st)
    assert not [k for k in st["notified"] if k.startswith("review_passed:")]
