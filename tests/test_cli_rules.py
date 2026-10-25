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
