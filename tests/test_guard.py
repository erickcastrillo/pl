"""The conftest guard: no test starts a real pl manager or dispatcher, however it is wrapped, and HOME is a temp dir.
Every command here names a path that does not exist, so a missing guard fails the test without running anything."""
import os
import subprocess
from pathlib import Path

import pytest

NO_PY, NO_PL = "/nonexistent/bin/python3.12", "/nonexistent/bin/pl"


@pytest.mark.parametrize("argv", [
    [NO_PY, "-m", "pl", "manager", "run"],
    [NO_PY, "-m", "pl", "dispatch", "--once"],
    [NO_PY, "-m", "pl.cli", "--profile", "work", "dispatch"],
    ["env", "-i", "PL_MANAGED=1", NO_PY, "-m", "pl", "manager", "start"],
    ["nohup", NO_PL, "manager", "run"],
    [NO_PL, "--profile=work", "dispatch"],
    ["sh", "-c", f"cd /tmp && {NO_PL} manager run &"],
    ["/bin/bash", "-lc", f"exec {NO_PY} -m pl dispatch"],
])
def test_subprocess_that_runs_pl_manager_or_dispatch_is_refused(argv):
    with pytest.raises(AssertionError, match="real pl"):
        subprocess.run(argv, capture_output=True)
    with pytest.raises(AssertionError, match="real pl"):
        subprocess.Popen(argv)


def test_a_shell_string_that_runs_pl_dispatch_is_refused():
    with pytest.raises(AssertionError, match="real pl"):
        subprocess.run(f"{NO_PY} -m pl dispatch", shell=True, capture_output=True)


@pytest.mark.parametrize("call", [
    lambda: os.posix_spawn(NO_PY, [NO_PY, "-m", "pl", "manager", "run"], os.environ),
    lambda: os.posix_spawnp(NO_PL, [NO_PL, "dispatch"], os.environ),
    lambda: os.execv(NO_PY, [NO_PY, "-m", "pl", "manager", "run"]),
    lambda: os.execlp(NO_PL, NO_PL, "dispatch"),
    lambda: os.spawnv(os.P_WAIT, NO_PL, [NO_PL, "manager", "run"]),
    lambda: os.spawnl(os.P_WAIT, NO_PY, NO_PY, "-m", "pl", "dispatch"),
])
def test_os_spawn_and_exec_that_run_pl_manager_or_dispatch_are_refused(call):
    with pytest.raises(AssertionError, match="real pl"):
        call()


@pytest.mark.parametrize("argv", [["echo", "pl", "manager"], [NO_PL, "list"], [NO_PY, "-m", "pl", "ideas", "dispatch"],
                                  [NO_PY, "-m", "pl", "dispatch", "--help"], [NO_PL, "manager", "-h"]])
def test_other_commands_are_not_refused(argv):
    try:
        subprocess.run(argv, capture_output=True)
    except FileNotFoundError:
        pass


def test_home_is_a_temp_dir(tmp_path_factory):
    base = os.path.realpath(tmp_path_factory.getbasetemp())
    assert os.path.realpath(os.environ["HOME"]).startswith(base + os.sep)
    assert os.path.realpath(Path.home()).startswith(base + os.sep)


def test_a_test_can_still_set_its_own_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert Path.home() == tmp_path


def test_the_real_gh_is_refused_and_a_fake_one_runs(tmp_path, monkeypatch):
    """Every console mounts Settings, whose worker ran the real gh auth status in each Textual test: refused now."""
    with pytest.raises(AssertionError, match="real harness or gh CLI"):
        subprocess.run(["gh", "auth", "status"], capture_output=True)
    fake = tmp_path / "gh"
    fake.write_text("#!/bin/sh\necho fake gh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    assert subprocess.run(["gh", "auth", "status"], capture_output=True, text=True).stdout == "fake gh\n"


def test_config_paths_are_under_the_temp_home(tmp_path_factory):
    from pl import config as C
    base = os.path.realpath(tmp_path_factory.getbasetemp()) + os.sep
    for name in ("HOME", "ATTN", "STATE_DIR", "PLANS", "LOCK_FILE", "WORK_DIR"):
        assert os.path.realpath(getattr(C, name)).startswith(base), name


def test_config_starts_from_the_defaults():
    from pl import config as C
    assert C.PIPELINE is None and C.CONFIG_DIR is None
    assert C.GATES == C._defaults()["GATES"]
    C.PIPELINE = {"stages": {}}   # leaks on purpose: the next test must not see it


def test_a_leaked_config_value_does_not_reach_the_next_test():
    from pl import config as C
    assert C.PIPELINE is None
