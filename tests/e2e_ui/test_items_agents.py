"""Items 10, 16a, 16m, 16g, 16b and 16k: the Assistant account, the rework message, the card screen's finding age, Esc from a card screen, `pl usage --github`."""
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from conftest import HERE, press
from pl import assistant, dispatch, harnesses, trackers
from pl import config as C
from test_tui_views import screen_text
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
