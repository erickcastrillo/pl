"""Items 10, 16a, 16m, 16g, 16b and 16k: the Assistant account, the rework message, the card screen's finding age, Esc from a card screen, `pl usage --github`."""
import json
import os
import argparse
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest
from textual.widgets import Footer
from textual.widgets._footer import FooterKey

from conftest import HERE, press
from pl import assistant, cli, dispatch, ghquota, harnesses, trackers, usage
from pl.tui import prs as tui_prs
from pl.tui.cards import CardScreen, CardsView
from pl import config as C
from test_tui_views import FakeGh, screen_text
from test_gh_rate_limit import gh, mine, tokens  # noqa: F401  (the fake gh program and the token fixtures of the rate-limit tests)
from tui_wait import settle, wait_until

FAKE_TMUX = """#!{python}
import json, sys
here = {folder!r}
argv = sys.argv[1:]
with open(here + "/tmux.log", "a") as f:
    f.write(json.dumps(argv) + "\\n")
state = json.load(open(here + "/tmux.json"))
panes = state.setdefault("panes", {{}})
flag = lambda name: argv[argv.index(name) + 1] if name in argv else None
cmd = argv[0] if argv else ""
if cmd == "new-window":
    n = len(panes) + 1
    panes[f"%{{n}}"] = {{"window": f"@{{n}}", "name": flag("-n"), "command": "claude", "screen": ""}}
    print(f"@{{n}}")
elif cmd == "list-panes":
    print(next(p for p, v in panes.items() if v["window"] == flag("-t")))
elif cmd == "display-message":
    pane = panes.get(flag("-t"))
    if pane is None:
        sys.exit(1)
    fmt = argv[-1]
    print(f"{{state['session']}} {{pane['window']}} {{pane['name']}}" if "session_name" in fmt else
          pane["command"] if "pane_current_command" in fmt else state["session"])
elif cmd == "capture-pane":
    pane = panes.get(flag("-t"))
    print(pane["screen"] if pane else "")
elif cmd == "kill-window":
    for p in [p for p, v in panes.items() if v["window"] == flag("-t")]:
        del panes[p]
json.dump(state, open(here + "/tmux.json", "w"))
"""


class FakeTmux:
    """A `tmux` program first on PATH: logs every argv, answers from tmux.json, so the code under test uses its real tmux calls."""

    def __init__(self, folder):
        self.folder = folder
        (folder / "bin").mkdir(parents=True)
        self.state = {"session": C.TMUX_SESSION, "panes": {}}
        self.save()
        program = folder / "bin" / "tmux"
        program.write_text(FAKE_TMUX.format(python=sys.executable, folder=str(folder)))
        program.chmod(0o700)
        gh_program = program.parent / "gh"   # the Dashboard's `gh search prs` has no seam: a program that finds no pull requests
        gh_program.write_text("#!/bin/sh\necho '[]'\n")
        gh_program.chmod(0o700)
        self.bin = program.parent

    def save(self):
        (self.folder / "tmux.json").write_text(json.dumps(self.state))

    def pane(self, pane, **fields):
        self.state = json.loads((self.folder / "tmux.json").read_text())
        self.state["panes"].setdefault(pane, {"window": "@1", "name": "run", "command": "claude", "screen": ""}).update(fields)
        self.save()

    def log(self):
        path = self.folder / "tmux.log"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def typed(self, pane):
        """[(text, Enter followed)] for each literal send-keys into pane."""
        lines, out = [a for a in self.log() if a[0] == "send-keys" and a[a.index("-t") + 1] == pane], []
        for i, a in enumerate(lines):
            if "-l" in a:
                out.append((a[-1], i + 1 < len(lines) and lines[i + 1][-1] == "Enter"))
        return out


@pytest.fixture
def profile(profile, tmp_path, monkeypatch):
    """The slice 1 profile with three accounts: nocli (codex, no codex program), a and b (claude)."""
    accounts = "".join(f'\n[accounts.{n}]\nharness = "{h}"\nconfig_dir = "{tmp_path / f".{n}-e2e"}"\n'
                       for n, h in (("nocli", "codex"), ("a", "claude"), ("b", "claude")))
    (profile / "config.toml").write_text('[tracker]\ntype = "local"\n' + accounts)
    monkeypatch.setattr(harnesses, "_run", lambda argv, **kw: subprocess.run(argv, capture_output=True, text=True, **kw))
    C.load()
    trackers.reset()   # the board's SQLite connection belongs to the thread that made it: the console's workers open their own
    return profile


@pytest.fixture
def tmux(profile, tmp_path, monkeypatch):
    fake = FakeTmux(tmp_path / "faketmux")
    monkeypatch.setenv("PATH", f"{fake.bin}{os.pathsep}{os.environ['PATH']}")
    return fake


async def open_assistant(pilot, account):
    await press(pilot, "2")
    await wait_until(pilot, lambda: assistant.load().get("account"), "the Assistant to start", timeout=20.0)
    await settle(pilot)
    assert assistant.load()["account"] == account, f"the Assistant started on {assistant.load()['account']}, which cannot start here"


async def test_item_10_assistant_starts_on_an_account_that_can_start(console, tmux):
    async with console() as pilot:
        await open_assistant(pilot, "a")
        text = screen_text(pilot.app).lower()
        assert not any(w in text for w in ("did not start", "error", "cannot be started")), text
    log = tmux.log()
    assert any(a[0] == "new-window" and "assistant" in a for a in log)
    pane = assistant.load()["pane"]
    assert any(re.fullmatch(r"sh \S+", text) and enter for text, enter in tmux.typed(pane))


async def test_item_10_open_console_uses_a_changed_assistant_account(console, tmux):
    async with console() as pilot:
        config = C.path()
        config.write_text(config.read_text() + '\n[assistant]\naccount = "a"\n')
        await open_assistant(pilot, "a")
        config.write_text(config.read_text().replace('account = "a"\n', 'account = "b"\n'))
        await press(pilot, "ctrl+r")
        await settle(pilot)
        await press(pilot, "y")
        await wait_until(pilot, lambda: assistant.load().get("account") == "b", "the Assistant to start on b", timeout=20.0)
        await settle(pilot)
        assert "(b)" in pilot.app.query_one("#assistant-view").status


PR = "https://github.com/o/r/pull/7"


def iso(minutes_ago):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


def where(request, screen, tab=None, key=None):
    """What the report says for a check that has no console: the slice 1 recorder reads this like it reads press()."""
    HERE.setdefault(request.node.name, {"start": False})["where"] = {"tab": tab, "screen": screen, "key": key}


@pytest.fixture
def dispatcher_pass(profile, tmux, monkeypatch, request):
    """A card in rework whose run agent sits in fake pane %1; `run(busy)` is one real dispatcher pass on the throwaway board."""
    where(request, "dispatcher pass")
    for name in ("start_worker", "sweep_untracked", "ensure_services", "notify", "mirror_to_product"):
        monkeypatch.setattr(dispatch, name, lambda *a, **k: None)

    def make(busy):
        worker = {"stage": "run", "harness": "claude", "pane": "%1", "window": "@1", "session_id": "s1", "started_at": iso(10)}
        if not busy:
            worker["finished_turn"] = True
        board = trackers.get("tracker")
        card = board.create("In progress", title="rework card", description="# PIPELINE: PLAN\nWP1", metadata={
            "pipeline_mode": "auto", "profile": "a", "worker": worker,
            "rework": {"active": True, "round": 1, "prs": [PR], "at": iso(1)}})
        tmux.pane("%1", window="@1", name="run", command="claude", screen="some output\n❯ ")
        monkeypatch.setattr(dispatch, "registry", lambda: {"s1": {"status": "busy" if busy else "idle"}})
        return card["id"]

    def run(card_id):
        dispatch.dispatch_once(1, False, pull=False)
        return trackers.get("tracker").card(card_id)["metadata"]["rework"]
    return make, run


def test_item_16a_idle_run_agent_gets_the_rework_message_once(dispatcher_pass, tmux):
    make, run = dispatcher_pass
    rework = run(make(busy=False))
    typed = tmux.typed("%1")
    assert len(typed) == 1 and "round 1" in typed[0][0] and typed[0][1], typed
    assert rework.get("sent_round") == 1


def test_item_16m_second_pass_in_the_same_round_types_nothing(dispatcher_pass, tmux):
    make, run = dispatcher_pass
    card_id = make(busy=False)
    run(card_id)
    run(card_id)
    assert len(tmux.typed("%1")) == 1


def test_item_16a_busy_run_agent_gets_nothing(dispatcher_pass, tmux):
    make, run = dispatcher_pass
    rework = run(make(busy=True))
    assert tmux.typed("%1") == []
    assert "sent_round" not in rework


def pr_card(monkeypatch, finding_minutes_ago, push_minutes_ago):
    """A PR open card whose review finding and last push are the given ages; `gh pr view` answers the push time."""
    board = trackers.get("tracker")
    card = board.create("PR open", title="card with a finding", metadata={
        "pipeline_mode": "auto", "profile": "a", "prs": [PR], "pr_urls": [PR],
        "rework": {"active": True, "round": 1, "at": iso(finding_minutes_ago)}})
    pushed = iso(push_minutes_ago)
    view = {"title": "feat: x", "author": {"login": "octo"}, "headRefName": "feat/x", "baseRefName": "main", "headRefOid": "abc123",
            "updatedAt": pushed, "commits": [{"committedDate": pushed, "oid": "abc123"}], "labels": [], "statusCheckRollup": [],
            "additions": 1, "deletions": 0, "changedFiles": 1, "reviewDecision": "", "mergeable": "MERGEABLE", "body": ""}
    monkeypatch.setattr(tui_prs, "_run", FakeGh(view=view))
    trackers.reset()
    return card["id"]


async def open_card_screen(pilot, card_id):
    """3, the Board sub-tab, the card's row, Enter."""
    app = pilot.app
    await press(pilot, "3")
    await wait_until(pilot, lambda: app.active_group() == "work", "the Work tab", timeout=20.0)
    app.action_tab("cards")

    def board():
        return app.query_one(CardsView)

    await wait_until(pilot, lambda: board()._rows and card_id in {r["card"]["id"] for r in board()._rows.values() if r.get("card")},
                     "the card on the board", timeout=20.0)
    for _ in range(40):
        if (row := board()._row()) and row["card"]["id"] == card_id:
            break
        await press(pilot, "down")
    await press(pilot, "enter")
    await wait_until(pilot, lambda: isinstance(app.screen, CardScreen) and app.screen.card is not None, "the card screen", timeout=20.0)


@pytest.mark.parametrize("finding_after_push,said", [(True, r"(?i)(newer|after).{0,40}last push"), (False, r"(?i)(older|before).{0,40}last push")],
                         ids=["newer", "older"])
async def test_item_16g_card_screen_says_finding_newer_or_older_than_last_push(console, tmux, monkeypatch, finding_after_push, said):
    card_id = pr_card(monkeypatch, *((20, 30) if finding_after_push else (30, 20)))
    async with console() as pilot:
        await open_card_screen(pilot, card_id)
        await settle(pilot)
        text = screen_text(pilot.app)
        other = r"(?i)(older|before).{0,40}last push" if finding_after_push else r"(?i)(newer|after).{0,40}last push"
        assert re.search(said, text), "the card screen does not say whether the finding is newer or older than the last push"
        assert not re.search(other, text)


async def test_item_16b_esc_twice_from_a_card_screen_keeps_the_console(console, tmux):
    card_id = trackers.get("tracker").create("In progress", title="esc card")["id"]
    trackers.reset()
    async with console() as pilot:
        app = pilot.app
        await open_card_screen(pilot, card_id)
        names_esc = "escape" in [k.key for k in app.query_one(Footer).query(FooterKey)]
        await press(pilot, "escape")
        await pilot.pause()
        await settle(pilot)
        assert len(app.screen_stack) == 1 and app.active_group() == "work", "the first Esc did not return to the board"
        await press(pilot, "escape")
        await pilot.pause()
        await settle(pilot)
        if not names_esc:
            assert len(app.screen_stack) == 1 and app.active_group() == "work" and app.active_tab == "cards"
        logs = "".join(p.read_text(errors="replace") for p in C.STATE_DIR.rglob("*.log"))
        assert app.is_running and app._exception is None
        assert "ScreenStackError" not in screen_text(app) + logs


def spend_budget(fake_gh):
    """GitHub's own count: each board read costs 3 points; the fake gh answers with the remaining budget after it."""
    reset = int(time.time()) + 1800
    state = {"spent": 0, "reset": reset}

    def read(do):
        state["spent"] += 3
        left = 5000 - state["spent"]
        rate = {"cost": 3, "remaining": left, "resetAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(reset)), "limit": 5000}
        fake_gh.set(rate=rate, remaining=left, reset=reset)
        (C.STATE_DIR / "board-cache.json").unlink(missing_ok=True)   # a read that GitHub answers, not one the cache did
        trackers.reset()
        do()
    return read, state


@pytest.mark.usefixtures("mine")
def test_item_16k_usage_github_names_each_pl_list_caller(request, monkeypatch, capsys):
    fake_gh = request.getfixturevalue("gh")
    where(request, "pl usage --github")
    monkeypatch.setenv("PL_CONFIG_DIR", str(C.CONFIG_DIR))
    monkeypatch.setattr(sys, "argv", ["pl", "list"])
    read, state = spend_budget(fake_gh)
    read(lambda: (ghquota.set_role("dispatcher"), trackers.get("tracker").cards()))
    read(lambda: (ghquota.set_role("console"), trackers.get("tracker").cards()))
    monkeypatch.setenv("PL_STAGE", "plan")
    monkeypatch.setenv("PL_CARD", "pl#1")
    read(lambda: cli.main())
    monkeypatch.delenv("PL_STAGE")
    monkeypatch.delenv("PL_CARD")
    read(lambda: cli.main())
    capsys.readouterr()
    assert fake_gh.set(remaining=5000 - state["spent"], reset=state["reset"]) is None
    usage.cmd_usage(argparse.Namespace(github=True, since=None, by=None, json=True))
    entry = next(e for e in json.loads(capsys.readouterr().out)["accounts"] if "acme-bot" in e["account"])
    points = {c["caller"].split(" ", 1)[-1]: c["points"] for c in entry["callers"]}
    assert points.get("dispatcher", 0) >= 3 and points.get("console", 0) >= 3 and points.get("pl list", 0) >= 3, points
    assert sum(points.values()) == state["spent"], points
    assert entry["outside_pl"] == 0
    usage.cmd_usage(argparse.Namespace(github=True, since=None, by=None, json=False))
    assert "outside pl 0" in capsys.readouterr().out.splitlines()[-1]
