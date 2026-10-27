"""The dispatcher's window sweep closes an exited agent's window once nothing in it needs a person: its card moved
on or has a newer agent. A window showing an error or a human gate is kept for 30 minutes."""
import subprocess
import time

import pytest

from pl import agents, alerts, dispatch, harnesses, notifications
from pl import config as C

ERROR_SCREEN = "Error: unknown option --foo\n$ "
CLOSING = "closing exited run agent window run-foo (@12) for pl#283: "


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PL_CONFIG_DIR", raising=False)
    d = tmp_path / ".pl-t"
    d.mkdir()
    (d / "config.toml").write_text("")
    C.load("t")
    C.STAGES = {"spec": {"account": "claude"}}
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


@pytest.fixture
def clock(monkeypatch):
    now = [time.time()]
    monkeypatch.setattr(time, "time", lambda: now[0])
    monkeypatch.setattr(alerts, "_clock", lambda: now[0])
    return now


def _card(col="PR open", window="@12", finished=(), tags=(), slug="foo", n=283):
    worker = {"window": window, "stage": "run"} if window else {}
    return {"id": f"o/pl#{n}", "title": f"Card {n}", "list_id": col, "tags": list(tags),
            "metadata": {"pipeline_mode": "auto", "spec_slug": slug, "worker": worker,
                         "finished_workers": [{"window": w, "stage": "run"} for w in finished]}}


def _row(wid="@12", name="run-foo", cmd="zsh", activity=0):
    return f"{wid} %{wid[1:]} {activity} - 0 {name} {cmd}\n"


class Sweep:
    def __init__(self, monkeypatch, capsys):
        self.mp, self.capsys = monkeypatch, capsys
        self.killed, self.notified, self.direct = [], [], []
        monkeypatch.setattr(dispatch, "death_line", agents.death_line)   # conftest fakes it for every other test
        monkeypatch.setattr(dispatch, "col_name", lambda lid: lid)
        monkeypatch.setattr(dispatch, "tmux", lambda *a, **k: self.killed.append(a[2]) if a[0] == "kill-window" else None)
        monkeypatch.setattr(notifications, "notify", lambda *a: self.notified.append(a))
        monkeypatch.setattr(dispatch, "notify", lambda *a: self.direct.append(a))

    def __call__(self, out, cards, screen="", st=None, dry=False):
        self.mp.setattr(subprocess, "run", lambda argv, *a, **kw: subprocess.CompletedProcess(argv, 0, out, ""))
        self.mp.setattr(harnesses, "_run", lambda argv, *a, **kw: subprocess.CompletedProcess(argv, 0, screen, ""))
        self.capsys.readouterr()
        dispatch.sweep_untracked(list(cards), {}, dry, st)
        return [ln for ln in self.capsys.readouterr().out.splitlines() if "exited" in ln]


@pytest.fixture
def sweep(monkeypatch, capsys):
    return Sweep(monkeypatch, capsys)


def test_an_exited_run_window_whose_card_is_in_pr_open_is_closed_with_one_line(sweep):
    lines = sweep(_row(), [_card()])
    assert sweep.killed == ["@12"]
    assert lines == [CLOSING + "card moved on"]


@pytest.mark.parametrize("card", [_card("In progress", tags=["parked"]), _card("Approved")])
def test_an_exited_window_of_a_held_card_or_a_run_card_back_in_approved_is_closed(sweep, card):
    lines = sweep(_row(), [card])
    assert sweep.killed == ["@12"]
    assert lines == [CLOSING + "card moved on"]


@pytest.mark.parametrize("finished", [False, True])
def test_an_old_window_is_closed_when_the_card_records_a_newer_agent(sweep, finished):
    c = _card("In progress", window="@20", finished=["@12"] if finished else ())
    lines = sweep(_row() + _row("@20", cmd="claude"), [c])
    assert sweep.killed == ["@12"]
    assert len(lines) == 1 and lines[0].endswith(": card has a newer agent")


def test_running_or_current_stage_windows_loops_and_dispatch_stay(sweep):
    out = _row(cmd="node") + _row("@13", "run-bar") + _row("@14", "loop-review") + _row("@15", "dispatch")
    cards = [_card("PR open", slug="foo"), _card("In progress", window="@13", slug="bar", n=284)]
    lines = sweep(out, cards)
    assert sweep.killed == [] and lines == []


def test_dry_run_says_would_close_and_closes_nothing(sweep):
    lines = sweep(_row(), [_card()], dry=True)
    assert sweep.killed == []
    assert lines == ["would " + CLOSING.replace("closing", "close") + "card moved on"]


def test_a_window_already_gone_gives_no_error_and_no_line(sweep):
    assert sweep("", [_card()], st={}) == []
    assert sweep.killed == []


def test_an_unowned_exited_window_keeps_the_old_sweep_rules(sweep, clock):
    assert sweep(_row("@30", "run-other", activity=int(clock[0])), [_card()]) == []
    assert sweep.killed == []


def test_an_exited_window_showing_an_error_is_kept_and_alerts_once(sweep, clock):
    st = {}
    assert sweep(_row(), [_card()], ERROR_SCREEN, st) == []
    assert sweep.killed == [] and list(st["exited_waits"]) == ["@12"]
    assert alerts.get("exited_window:@12")["count"] == 1 and len(sweep.notified) == 1 and sweep.direct == []
    clock[0] += 300
    sweep(_row(), [_card()], ERROR_SCREEN, st)
    assert sweep.killed == [] and len(sweep.notified) == 1


def test_a_human_gate_on_an_exited_window_keeps_it_and_a_nothing_runnable_gate_does_not(sweep):
    sweep(_row(), [_card()], "GATE: before-push\n$ ", {})
    assert sweep.killed == []
    lines = sweep(_row(), [_card()], "GATE: nothing-runnable\n$ ", {})
    assert sweep.killed == ["@12"] and lines == [CLOSING + "card moved on"]


def test_a_kept_window_closes_after_30_minutes_and_its_alert_clears(sweep, clock):
    st = {}
    sweep(_row(), [_card()], ERROR_SCREEN, st)
    clock[0] += 31 * 60
    lines = sweep(_row(), [_card()], ERROR_SCREEN, st)
    assert sweep.killed == ["@12"] and lines == [CLOSING + "kept 30 min for an error"]
    assert st["exited_waits"] == {}
    dispatch.check_alerts([], set(), st)
    assert alerts.get("exited_window:@12")["resolved_at"]


def test_the_keep_ignores_an_old_release_waiting_after(sweep, clock, monkeypatch):
    monkeypatch.setitem(C.DISPATCH, "release_waiting_after", 5)
    st = {}
    sweep(_row(), [_card()], ERROR_SCREEN, st)
    clock[0] += 10 * 60
    sweep(_row(), [_card()], ERROR_SCREEN, st)
    assert sweep.killed == []
    clock[0] += 21 * 60
    sweep(_row(), [_card()], ERROR_SCREEN, st)
    assert sweep.killed == ["@12"]


def test_a_kept_window_past_its_wait_in_dry_run_says_would_close(sweep, clock):
    st = {"exited_waits": {"@12": {"since": clock[0] - 31 * 60}}}
    lines = sweep(_row(), [_card()], ERROR_SCREEN, st, dry=True)
    assert lines == ["would " + CLOSING.replace("closing", "close") + "kept 30 min for an error"]
    assert sweep.killed == [] and alerts.get("exited_window:@12") is None


def test_the_error_rule_wins_over_a_newer_agent(sweep):
    c = _card("In progress", window="@20")
    sweep(_row() + _row("@20", cmd="claude"), [c], ERROR_SCREEN, {})
    assert sweep.killed == [] and alerts.get("exited_window:@12")
