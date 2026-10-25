"""CLI rules: exit 0 for done, 1 for failed work, 2 for bad input or setup, with a one-line reason on stderr."""
import copy
import sys

import pytest

from pl import board, cli, commands, trackers
from pl import config as C

CARD = {"id": "aaaa1111-0000-0000-0000-000000000001", "title": "card one", "description": "", "tags": [],
        "list_id": "col-Inbox", "updated_at": "", "metadata": {}}


class Tracker:
    fail_write = False

    def columns(self):
        return {t: f"col-{t}" for t in C.COLUMNS}

    def cards(self, query=None):
        return [copy.deepcopy(CARD)]

    def card(self, item_id):
        return copy.deepcopy(CARD)

    def update(self, item_id, *, verify=True, **fields):
        if "column" in fields:
            board.col_id(fields["column"])
        if self.fail_write:
            raise SystemExit("pl: the board refused the write\nstatus 500")
        return copy.deepcopy(CARD)


@pytest.fixture(autouse=True)
def profile(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("PL_CONFIG_DIR", "PL_TMUX_SESSION"):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / ".pl-t").mkdir()
    (tmp_path / ".pl-t" / "config.toml").write_text("")
    C.load("t")
    monkeypatch.setattr(commands, "registry", lambda: {})
    yield
    for k, v in saved.items():
        setattr(C, k, v)


def run(monkeypatch, capsys, argv, tracker):
    monkeypatch.setattr(trackers, "get", lambda name: tracker)
    monkeypatch.setattr(sys, "argv", ["pl", "--profile", "t", *argv])
    try:
        cli.main()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    else:
        code = 0
    return code, capsys.readouterr().err


@pytest.mark.parametrize("argv,expected", [
    (["card", "nosuch"], 2),
    (["move", "aaaa", "No column"], 2),
    (["idea", ""], 2),
    (["move", "aaaa", "Inbox"], 1),
    (["list"], 0),
])
def test_exit_codes(monkeypatch, capsys, argv, expected):
    t = Tracker()
    t.fail_write = argv[:1] == ["move"] and argv[2] == "Inbox"
    code, _ = run(monkeypatch, capsys, argv, t)
    assert code == expected


def test_reason_is_one_line_on_stderr(monkeypatch, capsys):
    t = Tracker()
    t.fail_write = True
    code, err = run(monkeypatch, capsys, ["move", "aaaa", "Inbox"], t)
    assert code == 1
    assert err == "pl: the board refused the write; status 500\n"
    code, err = run(monkeypatch, capsys, ["idea", ""], Tracker())
    assert code == 2 and err.count("\n") == 1 and err.startswith("pl idea:")


def test_a_reason_without_the_pl_prefix_gets_it(monkeypatch, capsys):
    t = Tracker()
    t.update = lambda *a, **k: (_ for _ in ()).throw(SystemExit("boom"))
    _, err = run(monkeypatch, capsys, ["move", "aaaa", "Inbox"], t)
    assert err == "pl: boom\n"
