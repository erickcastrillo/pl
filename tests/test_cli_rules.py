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
    monkeypatch.setattr(trackers, "_build", lambda name: tracker)
    monkeypatch.setattr(trackers, "_dry", False)
    monkeypatch.setattr(sys, "argv", ["pl", "--profile", "t", *argv])
    try:
        cli.main()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    else:
        code = 0
    captured = capsys.readouterr()
    run.out = captured.out
    return code, captured.err


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


class ReadOnlyTracker(Tracker):
    """Reads work; any write is a test failure."""

    def update(self, item_id, *, verify=True, **fields):
        raise AssertionError(f"wrote {item_id}: {fields}")

    create = delete = ensure_column = update

    def url(self, item_id):
        return f"https://board/{item_id}"


def _state_files():
    return sorted(str(p) for p in C.STATE_DIR.rglob("*")) if C.STATE_DIR.exists() else []


@pytest.mark.parametrize("argv", [
    ["move", "aaaa", "Approved"],
    ["done", "aaaa"],
    ["drop", "aaaa"],
    ["hold", "aaaa", "--reason", "later"],
    ["idea", "a brand new thing"],
    ["card", "aaaa", "--delete"],
], ids=lambda argv: argv[0])
def test_dry_run_writes_nothing(monkeypatch, capsys, argv):
    before = _state_files()
    code, err = run(monkeypatch, capsys, [*argv, "--dry-run"], ReadOnlyTracker())
    assert code == 0, err
    assert _state_files() == before


def test_dry_run_prints_what_it_would_do(monkeypatch, capsys):
    code, err = run(monkeypatch, capsys, ["move", "aaaa", "Approved", "--dry-run"], ReadOnlyTracker())
    out = run.out
    assert code == 0, err
    assert f"would update {CARD['id']}: column='Approved'" in out


def test_dry_run_cuts_card_text_and_masks_secrets(monkeypatch, capsys):
    text = "x" * 200 + " sk-" + "a" * 30
    run(monkeypatch, capsys, ["idea", text, "--dry-run"], ReadOnlyTracker())
    out = next(line for line in run.out.splitlines() if line.startswith("would create"))
    assert "x" * 61 not in out and "sk-" + "a" * 30 not in out
