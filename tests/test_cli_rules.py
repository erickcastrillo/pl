"""CLI rules: exit 0 for done, 1 for failed work, 2 for bad input or setup, with a one-line reason on stderr."""
import argparse
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
    trackers.set_dry_run(False)
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


WORKER = {"pane": "%7", "window": "@7", "session_id": "s1", "stage": "run"}


class WorkerTracker(ReadOnlyTracker):
    def card(self, item_id):
        return {**copy.deepcopy(CARD), "metadata": {"worker": dict(WORKER)}}

    def cards(self, query=None):
        return [self.card("")]


@pytest.mark.parametrize("argv", [["restart", "aaaa1111"], ["drop", "aaaa"], ["retry", "aaaa1111"]], ids=lambda argv: argv[0])
def test_dry_run_does_not_stop_a_running_agent(monkeypatch, capsys, argv):
    from pl import commands, move_agent
    sent = []
    monkeypatch.setattr(commands, "worker_status", lambda w, reg: ("alive", ""))
    monkeypatch.setattr(move_agent, "pane_refusal", lambda c, w: None)
    monkeypatch.setattr(move_agent, "_pane_cmd", lambda pane: "claude")
    monkeypatch.setattr(move_agent, "_run", lambda argv, **k: sent.append(argv))
    before = _state_files()
    code, err = run(monkeypatch, capsys, [*argv, "--dry-run"], WorkerTracker())
    assert code == 0, err
    assert sent == []
    assert _state_files() == before
    assert argv[0] == "retry" or "would stop" in run.out


def test_dry_run_approve_and_reject_write_no_memory_or_audit(monkeypatch, capsys):
    from pl import audit, recall
    writes = []
    monkeypatch.setattr(recall, "add", lambda *a, **k: writes.append("memory"))
    monkeypatch.setattr(audit, "_append_locked", lambda *a, **k: writes.append("audit"))
    t = ReadOnlyTracker()
    for argv in (["approve", "aaaa", "--force"], ["reject", "aaaa", "not good"]):
        code, err = run(monkeypatch, capsys, [*argv, "--dry-run"], t)
        assert code == 0, err
    assert writes == []
    assert not (C.STATE_DIR / "audit.jsonl").exists()


def _walk(entries):
    for e in entries:
        yield e
        yield from _walk(e["commands"])


def test_help_json_lists_every_subparser_and_pre_command(monkeypatch, capsys):
    import json
    import re
    monkeypatch.setattr(sys, "argv", ["pl", "--profile", "t", "help", "--json"])
    cli.main()
    top = {e["name"] for e in json.loads(capsys.readouterr().out)["commands"]}
    parser_names = set(next(a for a in cli.build_parser()._actions if a.dest == "cmd").choices)
    pre_names = set(re.findall(r'pre_a\.rest\[:1\] == \["(\w+)"\]', open(cli.__file__).read()))
    assert parser_names | pre_names <= top
    assert {"help", "jobs", "setup", "mcp"} <= top


def test_every_read_command_names_a_shape():
    from pl import jsonout
    found = {e["name"].replace(" ", "-"): e for e in _walk(jsonout.commands(cli.build_parser(), cli.PRE_COMMANDS))}
    for key, shape in jsonout.SHAPES.items():
        if key in found:
            assert found[key]["shape"] == shape
    assert found["list"]["shape"] == "board" and found["skills-list"]["shape"] == "skills-list"
    assert any(f["name"] == "--json" and f["type"] == "flag" for f in found["card"]["flags"])


@pytest.fixture
def machine(tmp_path, monkeypatch):
    monkeypatch.setenv("PL_MACHINE_DIR", str(tmp_path / "machine"))


def _jobs_json(capsys):
    import json
    cli.cmd_jobs(argparse.Namespace(json=True))
    return json.loads(capsys.readouterr().out)["jobs"]


def test_a_running_job_shows_in_pl_jobs_json_and_is_gone_after(machine, capsys):
    import os
    from pl import jobs
    with jobs.track("rework"):
        jobs.progress("sweeping")
        capsys.readouterr()
        (job,) = _jobs_json(capsys)
        assert job["name"] == "rework" and job["pid"] == os.getpid() and job["progress"] == "sweeping"
        assert oct(next((jobs.jobs_dir()).iterdir()).stat().st_mode & 0o777) == "0o600"
    assert _jobs_json(capsys) == []


def test_a_dead_pid_job_file_is_dropped(machine, capsys):
    import json
    from pl import jobs
    jobs.jobs_dir().mkdir(parents=True)
    stale = jobs.jobs_dir() / "e2e-1.json"
    stale.write_text(json.dumps({"id": "e2e-1", "name": "e2e", "pid": 2**31 - 1, "argv": [], "started": 0, "progress": None}))
    assert _jobs_json(capsys) == [] and not stale.exists()
