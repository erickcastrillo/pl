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


class _Closed:
    def isatty(self):
        return False

    def readline(self):
        return ""


@pytest.mark.parametrize("argv,flag", [
    (["move-agent", "aaaa", "t"], "--yes"),
    (["skills", "reset", "pl-run"], "--yes"),
    (["connectors", "add", "x", "--url", "https://x.example/mcp"], "--yes"),
])
def test_no_command_waits_for_input_with_stdin_closed(monkeypatch, capsys, argv, flag):
    import time
    from pl import move_agent
    monkeypatch.setattr(sys, "stdin", _Closed())
    (C.CONFIG_DIR / "config.toml").write_text("[accounts.t]\n")
    C.load("t")
    monkeypatch.setattr(move_agent, "move_card", lambda ref, target, confirm: confirm("Move it?"))
    start = time.monotonic()
    code, err = run(monkeypatch, capsys, argv, Tracker())
    assert code == 2 and flag in err and err.count("\n") == 1
    assert time.monotonic() - start < 1


def test_setup_prompt_without_a_terminal_names_the_flag(monkeypatch):
    from pl import setup
    from pl.util import BadInput
    monkeypatch.setattr(sys, "stdin", _Closed())
    with pytest.raises(SystemExit) as e:
        setup._input("Name: ")
    assert isinstance(e.value, BadInput) and "--yes" in str(e.value)


def test_no_bare_input_calls_left():
    import ast
    from pathlib import Path
    root = Path(cli.__file__).parent
    bare = [str(path.relative_to(root)) for path in root.rglob("*.py")
            for node in ast.walk(ast.parse(path.read_text()))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "input"]
    assert bare == ["util.py"]


def _skills_link(tmp_path, monkeypatch):
    acct = tmp_path / ".claude-a"
    acct.mkdir()
    (tmp_path / ".pl-t" / "config.toml").write_text(f'[accounts.a]\nconfig_dir = "{acct}"\n[skills]\nlibrary = "{tmp_path}/lib"\n')
    skill = tmp_path / "lib" / "my-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: my-skill\ndescription: d\n---\n")
    return ["skills", "link", "my-skill", "a"]


@pytest.mark.parametrize("what", ["profiles new", "skills link", "board init"])
def test_add_twice_changes_nothing_and_exits_0(monkeypatch, capsys, tmp_path, what):
    argv = {"profiles new": ["profiles", "new", "again"], "skills link": None, "board init": ["board", "init"]}[what]
    if argv is None:
        argv = _skills_link(tmp_path, monkeypatch)
    code, err = run(monkeypatch, capsys, argv, Tracker())
    assert code == 0, err
    assert "already exists" not in run.out or what == "board init"
    before = sorted(str(p) for p in tmp_path.rglob("*"))
    code, _ = run(monkeypatch, capsys, argv, Tracker())
    assert code == 0 and "already exists, unchanged" in run.out
    assert sorted(str(p) for p in tmp_path.rglob("*")) == before


def test_a_profile_with_other_content_is_still_refused_with_exit_2(monkeypatch, capsys, tmp_path):
    assert run(monkeypatch, capsys, ["profiles", "new", "other"], Tracker())[0] == 0
    (tmp_path / ".pl-other" / "config.toml").write_text("# edited\n")
    code, err = run(monkeypatch, capsys, ["profiles", "new", "other"], Tracker())
    assert code == 2 and "exists" in err
