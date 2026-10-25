"""Plan 22 slice 2: the dependency alert at pass end, and depends_on / waiting_on in `pl list --json`."""
import argparse
import json

import pytest

from pl import agents, alerts, commands, dispatch
from pl import config as C
from pl.agents import dep_broken, deps_open
from pl.trackers import github

SECRET = "SECRET card words"
DROPPED = "2026-10-23T12:00:00Z"


def _card(cid, col="Approved", **meta):
    return {"id": cid, "title": SECRET, "list_id": col, "description": SECRET, "metadata": {"pipeline_mode": "auto", **meta}}


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PL_CONFIG_DIR", raising=False)
    d = tmp_path / ".pl-t"
    d.mkdir()
    (d / "config.toml").write_text("")
    C.load("t")
    monkeypatch.setattr(agents, "col_name", lambda lid: lid)
    monkeypatch.setattr(dispatch, "col_name", lambda lid: lid)
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


@pytest.fixture
def clock(monkeypatch):
    t = {"now": 1_000_000.0}
    monkeypatch.setattr(alerts, "_clock", lambda: t["now"])
    monkeypatch.setattr(dispatch.time, "time", lambda: t["now"])
    return t


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(dispatch, "notify", lambda title, fix="": out.append((title, fix)))
    return out


@pytest.fixture
def gh(monkeypatch):
    calls = []
    state = {"reply": {"state": "closed", "merged": False}}

    def fake(args):
        calls.append(args)
        if isinstance(state["reply"], BaseException):
            raise state["reply"]
        return json.dumps(state["reply"])

    monkeypatch.setattr(github, "_gh", fake)
    dispatch._PR_STATE.clear()
    state["calls"] = calls
    return state


PR36 = "https://github.com/o/r/pull/360"


# ---------- WP1 ----------

def test_deps_open_lists_only_the_unmet_ids():
    done = _card("o/r#1", "Done")
    pr = _card("o/r#2", "PR open")
    dropped = _card("o/r#3", "Done", dropped_at=DROPPED)
    c = _card("o/r#4", depends_on=["o/r#1", "o/r#2", "o/r#3"])
    assert deps_open(c, [c, done, pr, dropped]) == ["o/r#2", "o/r#3"]
    assert deps_open(_card("o/r#5"), [c]) == []


def test_dep_broken_reads_drop_then_last_pr():
    states = {"u1": "closed", "u2": "open", "u3": "merged"}
    pr_state = states.get
    assert dep_broken(_card("o/r#1", "Done", dropped_at=DROPPED), pr_state) == "dropped"
    assert dep_broken(_card("o/r#1", "PR open", pr_urls=["u1"]), pr_state) == "closed"
    assert dep_broken(_card("o/r#1", "PR open", pr_urls=["u1", "u2"]), pr_state) is None
    assert dep_broken(_card("o/r#1", "PR open", pr_urls=["u3"]), pr_state) is None
    assert dep_broken(_card("o/r#1", "PR open", pr_urls=["nope"]), pr_state) is None
    assert dep_broken(None, pr_state) is None


# ---------- WP2 ----------

def _board(dep_col="Approved", **dep_meta):
    return [_card("o/r#37", depends_on=["o/r#36"]), _card("o/r#36", dep_col, **dep_meta)]


def test_a_dropped_dependency_opens_one_alert_naming_both_cards(clock, sent):
    cards = _board(dropped_at=DROPPED)
    dispatch.check_deps(cards)
    dispatch.check_deps(cards)
    a = alerts.get("dep_broken:o/r#37:o/r#36")
    assert a["count"] == 2
    assert "r#37" in a["title"] and "r#36" in a["title"]
    assert "pl undrop" in a["fix"] and "pl depends 'r#37' --clear" in a["fix"]
    assert len(sent) == 1
    clock["now"] += 3600
    dispatch.check_deps(cards)
    assert sent[-1][0].startswith("Still open after 1 h")


def test_a_pr_closed_unmerged_opens_the_alert(clock, sent, gh):
    cards = _board("PR open", pr_urls=[PR36])
    dispatch.check_deps(cards)
    assert "closed unmerged" in alerts.get("dep_broken:o/r#37:o/r#36")["title"]
    assert gh["calls"] == [["api", "repos/o/r/pulls/360"]]
    clock["now"] += 60
    dispatch.check_deps(cards)
    assert len(gh["calls"]) == 1


@pytest.mark.parametrize("cure", ["undrop", "done", "merged", "cleared"])
def test_the_alert_resolves_when_the_cause_clears(cure, clock, sent, gh):
    cards = _board("PR open", pr_urls=[PR36]) if cure == "merged" else _board(dropped_at=DROPPED)
    dispatch.check_deps(cards)
    key = "dep_broken:o/r#37:o/r#36"
    assert alerts._live(alerts.get(key))
    dep, dependent = cards[1], cards[0]
    if cure == "undrop":
        del dep["metadata"]["dropped_at"]
    elif cure == "done":
        del dep["metadata"]["dropped_at"]
        dep["list_id"] = "Done"
    elif cure == "merged":
        gh["reply"] = {"state": "closed", "merged": True}
        dispatch._PR_STATE.clear()
    else:
        dependent["metadata"]["depends_on"] = []
    dispatch.check_deps(cards)
    assert not alerts._live(alerts.get(key))


def test_a_dependency_not_yet_merged_opens_no_alert(clock, sent, gh):
    gh["reply"] = {"state": "open", "merged": False}
    dispatch.check_deps(_board("Approved"))
    dispatch.check_deps(_board("PR open", pr_urls=[PR36]))
    assert alerts.listing() == [] and sent == []


def test_a_card_outside_approved_gets_no_alert(clock, sent):
    cards = _board(dropped_at=DROPPED)
    cards[0]["list_id"] = "Done"
    dispatch.check_deps(cards)
    assert alerts.listing() == [] and sent == []


def test_the_alert_holds_only_ids_title_and_fix(clock, sent):
    dispatch.check_deps(_board(dropped_at=DROPPED))
    assert SECRET not in (C.STATE_DIR / "alerts.json").read_text()
    assert all(SECRET not in t + f for t, f in sent)


def test_a_failed_pr_lookup_keeps_the_last_state(clock, sent, gh):
    gh["reply"] = SystemExit("gh failed")
    cards = _board("PR open", pr_urls=[PR36])
    dispatch.check_deps(cards)
    assert alerts.listing() == []
    gh["reply"] = {"state": "closed", "merged": False}
    dispatch.check_deps(cards)
    clock["now"] += dispatch.PR_STATE_EVERY + 1
    gh["reply"] = SystemExit("gh failed")
    dispatch.check_deps(cards)
    assert alerts._live(alerts.get("dep_broken:o/r#37:o/r#36"))


# ---------- WP3 ----------

@pytest.fixture
def listing(monkeypatch):
    cs = [_card("o/r#1", "Done"), _card("o/r#2", "PR open"),
          _card("o/r#3", "Approved", depends_on=["o/r#1", "o/r#2"]), _card("o/r#4", "Done", depends_on=["o/r#2"])]
    monkeypatch.setattr(commands, "col_name", lambda lid: lid)
    monkeypatch.setattr(agents, "col_name", lambda lid: lid)
    monkeypatch.setattr(commands, "cards", lambda: cs)
    monkeypatch.setattr(commands, "share", lambda *a: None)
    monkeypatch.setattr(commands, "registry", lambda: {})
    monkeypatch.setattr(commands, "worker_view", lambda c, reg: "")
    monkeypatch.setattr(commands, "approved_label", lambda c, col, reg, *p: None)
    monkeypatch.setattr(commands, "intake_configured", lambda: False)
    monkeypatch.setattr(commands, "exhausted_profiles", lambda: set())
    monkeypatch.setattr(commands, "paused", lambda: None)
    return cs


def _rows(capsys, **kw):
    commands.cmd_list(argparse.Namespace(**{"json": True, "all": False, "product": False, **kw}))
    return json.loads(capsys.readouterr().out)["pipeline"]


def test_list_json_rows_carry_depends_on_and_waiting_on(listing, capsys):
    rows = {r["id"]: r for r in _rows(capsys)}
    assert set(rows) == {"o/r#2", "o/r#3"}
    assert rows["o/r#2"]["depends_on"] == [] and rows["o/r#2"]["waiting_on"] == []
    assert rows["o/r#3"]["depends_on"] == ["o/r#1", "o/r#2"]
    assert rows["o/r#3"]["waiting_on"] == ["o/r#2"]
    assert {r["id"] for r in _rows(capsys, all=True)} == {"o/r#1", "o/r#2", "o/r#3", "o/r#4"}


def test_list_json_stays_json_when_paused_and_parked(listing, monkeypatch, capsys):
    monkeypatch.setattr(commands, "paused", lambda: {"since": "x"})
    monkeypatch.setattr(commands, "exhausted_profiles", lambda: {"acme"})
    _rows(capsys)


def test_list_without_json_is_unchanged(listing, monkeypatch, capsys):
    monkeypatch.setattr(commands, "exhausted_profiles", lambda: {"acme"})
    monkeypatch.setattr(commands, "parking", lambda p: {})
    commands.cmd_list(argparse.Namespace(all=False, product=False))
    out = capsys.readouterr().out
    assert out.startswith("PROFILES parked") and "FEATURE PIPELINE board" in out
