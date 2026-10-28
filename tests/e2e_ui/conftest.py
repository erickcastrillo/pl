"""The console acceptance suite behind `pl e2e console`: a real PlApp on a throwaway profile, one result line per test."""
import contextlib
import json
import os
import re
import sqlite3
import subprocess
from pathlib import Path

import pytest
from textual.widgets import Footer

from pl import config as C
from pl import harnesses, trackers, watch
from pl.tui import prs as tui_prs
from pl.tui.app import PlApp
from test_tui_views import FakeGh, screen_text
from tui_wait import wait_until

ITEM = re.compile(r"^test_item_(\d+[a-q]?)_")
TAB = re.compile(r"^test_tab\[(\w+)\]")
HERE = {}   # test name -> {where, screen, start}: what the console showed when the test went wrong


def pytest_configure(config):
    config.addinivalue_line("markers", "e2e_ui: console acceptance suite (pl e2e console; run with -m e2e_ui)")


def pytest_collection_modifyitems(config, items):
    skip = pytest.mark.skip(reason="run with -m e2e_ui")
    for item in items:
        if Path(str(item.fspath)).parent.name == "e2e_ui":
            item.add_marker(pytest.mark.e2e_ui)
            if "e2e_ui" not in config.option.markexpr:
                item.add_marker(skip)


@pytest.fixture
def profile(tmp_path, monkeypatch):
    """A throwaway profile: a local board with a card per main column, one fake claude account, nothing real reachable."""
    # the console's refresh worker and the test both touch the local board: let its SQLite connection cross threads
    _connect = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **kw: _connect(*a, **{**kw, "check_same_thread": False}))
    folder = tmp_path / ".pl-e2e"
    folder.mkdir()
    account = tmp_path / ".claude-e2e"
    (folder / "config.toml").write_text(
        f'[tracker]\ntype = "local"\n\n[accounts.e2e]\nharness = "claude"\nconfig_dir = "{account}"\n')
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for fake in ("tmux", "claude"):   # the root conftest allows only fakes under the base temp folder
        (bin_dir / fake).write_text("#!/bin/sh\nexit 1\n")
        (bin_dir / fake).chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PL_CONFIG_DIR", str(folder))
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    C.load()
    trackers.reset()
    board = trackers.get("tracker")
    for column in board.columns():
        board.create(column, title=f"e2e card in {column}")
    trackers.reset()   # the console's refresh worker opens its own connection: a SQLite one cannot cross threads
    monkeypatch.setattr(tui_prs, "_run", FakeGh(), raising=False)
    monkeypatch.setattr(watch, "pr_activity", lambda days=14: None)   # the refresh worker would run `gh search prs`
    monkeypatch.setattr(harnesses, "_run", lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", ""))
    yield folder
    trackers.reset()
    for k, v in saved.items():
        setattr(C, k, v)


@pytest.fixture
def console(profile, request):
    """`async with console() as pilot`: the real console on the throwaway profile; a failure keeps what was on screen."""
    name = request.node.name

    @contextlib.asynccontextmanager
    async def start():
        app = PlApp(autostart=False, whatsnew=False, updates=False, restarts=False)
        app.e2e_test = name
        here = HERE.setdefault(name, {"where": {}, "start": True})
        try:
            async with app.run_test(size=(140, 45)) as pilot:
                await wait_until(pilot, lambda: pilot.app.query(Footer), "the console to mount", timeout=20.0)
                here["start"] = False
                try:
                    yield pilot
                except BaseException:
                    here["text"] = screen_text(app)
                    raise
        finally:
            if here["start"]:
                here["text"] = "the console did not start"
    return start


async def press(pilot, *keys):
    for key in keys:
        app = pilot.app
        HERE[app.e2e_test]["where"] = {"tab": app.active_group(), "screen": type(app.screen).__name__, "key": key}
        await pilot.press(key)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    rep = yield
    out = os.environ.get("PL_E2E_UI_OUT")
    if not out or rep.when == "teardown" or (rep.when == "setup" and not rep.failed):
        return rep
    here = HERE.get(item.name, {})
    record = {"test": item.name, "outcome": rep.outcome, "reason": _reason(rep), "where": here.get("where", {}), "screen": ""}
    if (m := ITEM.match(item.name)):
        record["item"] = m.group(1)
    elif (m := TAB.match(item.name)):
        record["tab"] = m.group(1)
    if here.get("start") and rep.failed:
        record["item"] = "start"
    if rep.failed and here.get("text"):
        stem = re.sub(r"\W+", "_", item.name).strip("_")
        shot = Path(out) / "screens" / f"{stem}.txt"
        shot.parent.mkdir(parents=True, exist_ok=True)
        shot.write_text(here["text"])
        record["screen"] = f"screens/{stem}.txt"
    with open(Path(out) / "results.jsonl", "a") as f:
        f.write(json.dumps(record) + "\n")
    return rep


def _reason(rep):
    if rep.skipped:
        return str(rep.longrepr[2]).removeprefix("Skipped: ")
    return str(rep.longrepr.reprcrash.message).splitlines()[0][:200] if rep.failed and hasattr(rep.longrepr, "reprcrash") else ""
