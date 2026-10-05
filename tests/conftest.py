import os
import re
import shutil
import subprocess

import pytest

from pl import memory


@pytest.fixture(autouse=True)
def no_real_memory_readers(monkeypatch):
    """No test reads the real vm_stat, ps, /proc or tmux, or sends a signal: memory reads as unknown."""
    def no_kill(pid, sig):
        raise AssertionError(f"a test tried to signal pid {pid}")
    monkeypatch.setattr(memory, "_run", lambda argv: None)
    monkeypatch.setattr(memory, "_meminfo", lambda: None)
    monkeypatch.setattr(memory, "_kill", no_kill)
    memory._CACHE.clear()
    monkeypatch.delenv("PL_MACHINE_DIR", raising=False)   # pl manager's folder: always under the test's own HOME


@pytest.fixture(autouse=True)
def temp_home(monkeypatch, tmp_path_factory):
    """HOME is a temporary folder for every test; a test that sets its own HOME afterwards still wins."""
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))
    monkeypatch.setenv("PL_NO_UPDATE_CHECK", "1")   # no test reads GitHub's tags; test_update.py turns it back on
    from pl import local_model

    def no_model(url, payload, timeout):
        raise OSError("tests never reach a local model")
    monkeypatch.setattr(local_model, "_request", no_model)   # a test that needs answers fakes _request itself
    from pl import accounts

    def no_probe(argv, env, timeout, cwd):
        raise OSError("tests never run a harness CLI")
    monkeypatch.setattr(accounts, "_probe", no_probe)        # a parked account's health check; tests fake it


PY = re.compile(r"python[\d.]*$")
WRAPPERS = ("env", "nohup", "exec", "command")
SHELLS = ("sh", "bash", "zsh", "dash")
HARNESS_BINS = ("claude", "codex", "agy", "gemini")   # the harness CLIs: a test never runs the real one
# pl manager / pl dispatch in a shell string: the console script, or python -m pl
PL_SHELL = re.compile(r"(?:(?:^|[\s/;&|(`])pl|-m\s+pl(?:\.cli)?)\s+(?:--profile[=\s]\S+\s+)?(?:manager|dispatch)\b")


def _unwrap(a):
    """argv without leading env [-i] [NAME=value ...] / nohup / exec wrappers."""
    while a and os.path.basename(a[0]) in WRAPPERS:
        a = a[next((i for i, x in enumerate(a) if i and not x.startswith("-") and "=" not in x), len(a)):]
    return a


def _shell_cmd(a):
    """The command string of sh -c / bash -lc ..., or None."""
    if a and os.path.basename(a[0]) in SHELLS:
        for i, x in enumerate(a[1:], 1):
            if x.startswith("-") and not x.startswith("--") and "c" in x:
                return " ".join(a[i + 1:])
    return None


def _runs_pl(a):
    """argv (unwrapped) runs pl manager or pl dispatch: python -m pl[.cli] ... or the pl console script."""
    if not a:
        return False
    b = os.path.basename(a[0])
    if PY.match(b):
        i = a.index("-m") if "-m" in a else len(a)
        if a[i + 1:i + 2] not in (["pl"], ["pl.cli"]):
            return False
        rest = a[i + 2:]
    elif b == "pl":
        rest = a[1:]
    else:
        return False
    it = iter(rest)
    for x in it:
        if x == "--profile":
            next(it, None)
        elif not x.startswith("-"):
            return x in ("manager", "dispatch") and not {"-h", "--help"} & set(rest)   # --help only prints
    return False


def _argv(argv):
    return [os.fsdecode(x) if isinstance(x, bytes) else str(x) for x in argv if isinstance(x, (str, bytes, os.PathLike))]


@pytest.fixture(autouse=True)
def no_real_tmux(monkeypatch, tmp_path_factory):
    """A test that shells out to a real tmux binary fails: every tmux call goes through a faked seam, or runs a fake
    tmux script the test put in its own temporary folder. A test that runs pl manager or pl dispatch (the manager is
    on by default) fails however it is wrapped: env, nohup, sh -c, a shell string, os.posix_spawn, os.spawn*, os.exec*."""
    real_run, real_popen = subprocess.run, subprocess.Popen
    fakes = os.path.realpath(tmp_path_factory.getbasetemp())

    def real(cmd):
        raise AssertionError(f"a test ran the real tmux: {cmd!r}")

    def real_pl(cmd):
        raise AssertionError(f"a test ran a real pl manager or dispatcher: {cmd!r}")

    def check(argv, kw):
        if isinstance(argv, (str, bytes, os.PathLike)):   # a shell string (shell=True) or a bare command name
            if kw.get("shell"):
                if PL_SHELL.search(os.fsdecode(argv)):
                    real_pl(argv)
                if re.search(r"\btmux\b", os.fsdecode(argv)):
                    real(argv)
            argv = [argv]
        a = _unwrap(_argv(argv))
        if _runs_pl(a):
            real_pl(a)
        cmd = _shell_cmd(a)
        if cmd is not None:
            if PL_SHELL.search(cmd):
                real_pl(a)
            if re.search(r"\btmux\b", cmd):
                real(a)
        if a and os.path.basename(a[0]) in HARNESS_BINS:
            found = shutil.which(a[0], path=(kw.get("env") or os.environ).get("PATH"))
            if not (found and os.path.realpath(found).startswith(fakes + os.sep)):
                raise AssertionError(f"a test ran a real harness CLI: {a!r}")
        if not a or os.path.basename(a[0]) != "tmux":
            return
        path = (kw.get("env") or os.environ).get("PATH")
        found = shutil.which(a[0], path=path)
        if not (found and os.path.realpath(found).startswith(fakes + os.sep)):
            real(a)

    def run(argv, *a, **kw):
        check(argv, kw)
        return real_run(argv, *a, **kw)

    class Popen(real_popen):
        def __init__(self, argv, *a, **kw):
            check(argv, kw)
            super().__init__(argv, *a, **kw)

    def shell_guard(fn):
        def guarded(cmd, *a, **kw):
            check(cmd, {"shell": True})
            return fn(cmd, *a, **kw)
        return guarded

    def argv_guard(fn, at, listed, env_last=False):
        """os.exec*/spawn*/posix_spawn*: the argv is the list at position at, or (the *l forms) the rest of the call."""
        def guarded(*args, **kw):
            argv = args[at] if listed else args[at:len(args) - env_last]
            env = args[at + 1] if listed and len(args) > at + 1 else args[-1] if env_last else kw.get("env")
            check([args[at - 1], *list(argv)[1:]], {"env": env if isinstance(env, dict) or hasattr(env, "get") else None})
            return fn(*args, **kw)
        return guarded

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "Popen", Popen)
    monkeypatch.setattr(os, "system", shell_guard(os.system))
    monkeypatch.setattr(os, "popen", shell_guard(os.popen))
    for name in ("posix_spawn", "posix_spawnp"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, argv_guard(getattr(os, name), 1, True))
    for name in ("execv", "execve", "execvp", "execvpe", "execl", "execle", "execlp", "execlpe"):
        monkeypatch.setattr(os, name, argv_guard(getattr(os, name), 1, name.startswith("execv"), name in ("execle", "execlpe")))
    for name in ("spawnv", "spawnve", "spawnvp", "spawnvpe", "spawnl", "spawnle", "spawnlp", "spawnlpe"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, argv_guard(getattr(os, name), 2, name.startswith("spawnv"),
                                                     name in ("spawnle", "spawnlpe")))


@pytest.fixture(autouse=True)
def no_live_agent_window(monkeypatch):
    """The dispatcher's look for an agent window it does not track reads tmux: off unless a test turns it on."""
    from pl import dispatch
    monkeypatch.setattr(dispatch, "live_agent_window", lambda *a, **k: False)
    monkeypatch.setattr(dispatch, "api_error_wait", lambda *a, **k: None)   # it reads the agent's screen: tests fake it
    monkeypatch.setattr(dispatch, "death_line", lambda *a, **k: None)
