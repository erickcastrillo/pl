"""Plan 6 slice 6: `pl loops run NAME --dry-run` runs the loop read-only and prints what it would do."""
import types

import pytest

from pl import accounts, harnesses, loops
from pl import config as C
from pl.util import BadInput


@pytest.fixture(autouse=True)
def profile(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PL_CONFIG_DIR", raising=False)
    C.load()
    C.PROFILES = {"cx": tmp_path / ".codex-cx"}
    C.ACCOUNTS = {"cx": {"harness": "codex", "config_dir": str(tmp_path / ".codex-cx")}}
    C.SERVICES = {"invoices": {"prompt": "/pl-billing-chase", "profile": "cx", "every": "daily 09:00", "writes": True}}
    monkeypatch.setattr(accounts, "exhausted_profiles", lambda: set())
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


def _args(**kw):
    return types.SimpleNamespace(loops_cmd="run", name="invoices", dry_run=True, **kw)


def test_dry_run_runs_read_only_and_prints_what_it_would_do(monkeypatch, capsys):
    ran = []

    def run(argv, **kw):
        ran.append((argv, kw["env"]))
        return types.SimpleNamespace(returncode=0, stderr="", stdout="looking\nwould file: Chase Acme\n3 invoices overdue\n")

    monkeypatch.setattr(harnesses, "_run", run)
    loops.cmd_loops(_args())
    (argv, env), = ran
    assert "read-only" in argv and "workspace-write" not in argv
    assert env["PL_DRY_RUN"] == "1" and env["PL_LOOP_WRITES"] == "0" and env["PL_LOOP"] == "invoices"
    out = capsys.readouterr().out
    assert "3 invoices overdue" in out and "would file: Chase Acme" in out
    assert not (C.STATE_DIR / "loops").exists()


def test_dry_run_times_out_with_a_message(monkeypatch):
    def run(argv, **kw):
        assert kw["timeout"] == loops.DRY_RUN_TIMEOUT
        raise harnesses.subprocess.TimeoutExpired(argv, kw["timeout"])

    monkeypatch.setattr(harnesses, "_run", run)
    with pytest.raises(BadInput, match="timed out"):
        loops.cmd_loops(_args())


def test_dry_run_needs_a_known_loop():
    with pytest.raises(BadInput, match="no loop named"):
        loops.cmd_loops(types.SimpleNamespace(loops_cmd="run", name="nope", dry_run=True))
