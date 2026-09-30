"""End-to-end: the real `pl` entry point and two real dispatcher processes side by side.
Fake `gh` and `tmux` binaries come first on PATH and every path is under tmp_path, so nothing touches the
real HOME, tmux, network or a harness. Skipped unless PL_E2E=1."""
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

pytestmark = [pytest.mark.e2e, pytest.mark.skipif(os.environ.get("PL_E2E") != "1", reason="set PL_E2E=1 to run")]


@pytest.fixture
def env(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("gh", "echo '[]'"), ("tmux", "exit 1")):   # empty board; no tmux session exists
        f = bin_dir / name
        f.write_text(f"#!/bin/sh\n{body}\n")
        f.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    e = {k: v for k, v in os.environ.items() if k not in ("PL_CONFIG_DIR", "PL_TMUX_SESSION")}
    e.update(HOME=str(home), PATH=f"{bin_dir}:{e['PATH']}", PYTHONDONTWRITEBYTECODE="1")
    return e


def _profile(tmp_path, name):
    d = tmp_path / name
    d.mkdir()
    (d / "config.toml").write_text(
        f'tmux_session = "pl-e2e-{name}"\n'
        '[tracker]\ntype = "github-issues"\nrepo = "example/board"\n'
        '[accounts.main]\nconfig_dir = "~/.claude-main"\n')
    return d


def _pl(env, config_dir, *args):
    return ["uv", "run", "--project", str(ROOT), "pl", *args], {**env, "PL_CONFIG_DIR": str(config_dir)}


def test_entry_point_runs(env):
    r = subprocess.run(["uv", "run", "--project", str(ROOT), "pl", "--help"], env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and "pl - the feature funnel" in r.stdout


def test_two_profiles_side_by_side(env, tmp_path):
    a, b = _profile(tmp_path, "a"), _profile(tmp_path, "b")
    procs = []
    try:
        for d in (a, b):
            argv, e = _pl(env, d, "dispatch", "--interval", "30", "--no-pull")
            procs.append(subprocess.Popen(argv, env=e, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True))
        time.sleep(3)
        assert [p.poll() for p in procs] == [None, None], "both dispatchers should still be running"
        argv, e = _pl(env, a, "dispatch", "--interval", "30", "--no-pull")
        second = subprocess.run(argv, env=e, capture_output=True, text=True, timeout=60)
        assert second.returncode == 0
        assert "a dispatcher is already running for this profile (pid " in second.stderr
    finally:
        for p in procs:
            if p.poll() is None:
                os.killpg(p.pid, signal.SIGTERM)
            p.wait(timeout=30)
