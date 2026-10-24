"""pl link: cards in different pipelines list each other in metadata.links."""
import argparse
import copy
import io
import subprocess
import sys
from contextlib import redirect_stdout

import pytest

from pl import cli, commands, links, trackers
from pl import config as C

CLIENT = "cccc1111-0000-0000-0000-000000000001"
ENG = "eeee2222-0000-0000-0000-000000000002"


class Board:
    def __init__(self, cid, title, column):
        self.items = {cid: {"id": cid, "title": title, "description": "", "tags": [], "list_id": f"col-{column}",
                            "updated_at": "", "metadata": {}}}

    def columns(self):
        return {t: f"col-{t}" for t in C.COLUMNS}

    def cards(self, query=None):
        return [copy.deepcopy(c) for c in self.items.values()]

    def card(self, item_id):
        return copy.deepcopy(self.items[item_id])

    def update(self, item_id, *, verify=True, **fields):
        c = self.items[item_id]
        if "metadata" in fields:
            c["metadata"] = {**c["metadata"], **fields.pop("metadata")}
        c.update(fields)
        return copy.deepcopy(c)

    def reset(self, *a):
        pass


@pytest.fixture
def two(tmp_path, monkeypatch):
    """Profiles sales and engineering, each with its own board; links._run runs pl for the other profile in this process."""
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("PL_CONFIG_DIR", "PL_TMUX_SESSION"):
        monkeypatch.delenv(var, raising=False)
    for name in ("sales", "engineering"):
        d = tmp_path / f".pl-{name}"
        d.mkdir()
        (d / "config.toml").write_text("")
    boards = {"sales": Board(CLIENT, "Acme onboarding", "Inbox"), "engineering": Board(ENG, "Build the portal", "Inbox")}
    monkeypatch.setattr(trackers, "get", lambda kind: boards[C.PROFILE_NAME])
    monkeypatch.setattr(links, "fresh_next", lambda: None)
    calls = []

    def run(argv):
        calls.append(argv)
        monkeypatch.setattr(sys, "argv", ["pl", *argv[3:]])
        try:
            cli.main()
            code = 0
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
        finally:
            C.load("sales")
        return subprocess.CompletedProcess(argv, code, "", "")

    monkeypatch.setattr(links, "_run", run)
    C.load("sales")
    yield boards, calls
    for k, v in saved.items():
        setattr(C, k, v)


def linked(board, cid):
    return board.items[cid]["metadata"].get("links")


def test_link_writes_both_cards_metadata_links(two):
    boards, calls = two
    links.add(CLIENT, "engineering", ENG, "built-for")
    assert linked(boards["sales"], CLIENT) == [{"pipeline": "engineering", "id": ENG, "relation": "built-for"}]
    assert linked(boards["engineering"], ENG) == [{"pipeline": "sales", "id": CLIENT, "relation": "built-for"}]
    assert calls[0][-1] == "--one-side" and "--profile" in calls[0] and not any(isinstance(x, list) for x in calls[0])


def test_link_is_idempotent(two):
    boards, _ = two
    links.add(CLIENT, "engineering", ENG, "built-for")
    links.add(CLIENT, "engineering", ENG, "built-for")
    assert len(linked(boards["sales"], CLIENT)) == 1
    assert len(linked(boards["engineering"], ENG)) == 1


def test_a_failed_other_side_rolls_back_this_side(two, monkeypatch):
    boards, _ = two
    monkeypatch.setattr(links, "_run", lambda argv: subprocess.CompletedProcess(argv, 1, "", "boom"))
    with pytest.raises(SystemExit) as e:
        links.add(CLIENT, "engineering", ENG, "built-for")
    assert "nothing was linked" in str(e.value)   # a message exits 1
    assert not linked(boards["sales"], CLIENT)


def test_unknown_pipeline_exits_2(two):
    boards, calls = two
    with pytest.raises(SystemExit) as e:
        links.add(CLIENT, "billing", ENG, "related")
    assert e.value.code == 2
    assert not calls and not linked(boards["sales"], CLIENT)


def test_pl_card_shows_links(two):
    links.add(CLIENT, "engineering", ENG, "built-for")
    out = io.StringIO()
    with redirect_stdout(out):
        commands.cmd_card(argparse.Namespace(id=CLIENT, delete=False))
    assert "links: engineering:eeee2222 (built-for)" in out.getvalue()
