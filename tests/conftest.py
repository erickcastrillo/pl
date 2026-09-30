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
def no_real_tmux(monkeypatch, tmp_path_factory):
    """A test that shells out to a real tmux binary fails: every tmux call goes through a faked seam, or runs a fake
    tmux script the test put in its own temporary folder."""
    real_run, real_popen = subprocess.run, subprocess.Popen
    fakes = os.path.realpath(tmp_path_factory.getbasetemp())
    shells = ("sh", "bash", "zsh", "dash")

    def real(cmd):
        raise AssertionError(f"a test ran the real tmux: {cmd!r}")

    def check(argv, kw):
        if isinstance(argv, (str, bytes)):   # a shell string (shell=True) or a bare command name
            if kw.get("shell") and re.search(r"\btmux\b", os.fsdecode(argv)):
                real(argv)
            argv = [argv]
        a = [os.fsdecode(x) if isinstance(x, bytes) else str(x) for x in argv]
        if a and os.path.basename(a[0]) == "env":   # env [-i] [NAME=value ...] tmux ...
            a = a[next((i for i, x in enumerate(a) if i and not x.startswith("-") and "=" not in x), len(a)):]
        if a and os.path.basename(a[0]) in shells and "-c" in a[1:]:
            cmd = " ".join(a[a.index("-c") + 1:])
            if re.search(r"\btmux\b", cmd):
                real(a)
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
            if re.search(r"\btmux\b", os.fsdecode(cmd)):
                real(cmd)
            return fn(cmd, *a, **kw)
        return guarded

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "Popen", Popen)
    monkeypatch.setattr(os, "system", shell_guard(os.system))
    monkeypatch.setattr(os, "popen", shell_guard(os.popen))
