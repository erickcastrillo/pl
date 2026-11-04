"""Fake CLIs answered in the test's own process: subprocess.run on a fake script a test put on PATH returns what the
script would print, without starting a process. The script file stays on disk, so PATH lookups and the conftest
guard see it as before."""
import builtins
import io
import os
import shutil
import subprocess
import sys
import types


class _Proxy(types.ModuleType):
    """A module that is the real one except for the attributes given."""

    def __init__(self, real, **own):
        super().__init__(real.__name__)
        self._real = real
        vars(self).update(own)

    def __getattr__(self, name):
        return getattr(self._real, name)


def script(source):
    """A fake CLI written as a Python script, run in-process: its own sys.argv, os.environ, stdout, stderr and exit
    code, as when it ran as `python script args...`. Returns handler(args, env) -> (code, stdout, stderr)."""
    code = compile(source, "<fake cli>", "exec")

    def handler(args, env):
        out, err = io.StringIO(), io.StringIO()
        fake_sys = _Proxy(sys, argv=["fake", *args], stdout=out, stderr=err)
        fake_os = _Proxy(os, environ=env)

        def imp(name, *a, **k):
            return {"sys": fake_sys, "os": fake_os}.get(name) or builtins.__import__(name, *a, **k)

        def prn(*a, file=None, **k):
            builtins.print(*a, file=out if file is None else file, **k)
        ns = {"__name__": "__main__", "__builtins__": {**vars(builtins), "__import__": imp, "print": prn}}
        try:
            exec(code, ns)
            rc = 0
        except SystemExit as e:
            rc = e.code if isinstance(e.code, int) else 0 if e.code is None else (err.write(f"{e.code}\n"), 1)[1]
        return rc, out.getvalue(), err.getvalue()
    return handler


def install(monkeypatch, fakes):
    """subprocess.run of an argv whose program is found on PATH at one of fakes' paths calls that path's handler."""
    fakes = {os.path.realpath(p): h for p, h in fakes.items()}
    names = {os.path.basename(p) for p in fakes}
    real = subprocess.run

    def run(argv, *a, **kw):
        if isinstance(argv, (list, tuple)) and argv and os.path.basename(str(argv[0])) in names:
            env = kw.get("env")
            env = dict(os.environ if env is None else env)
            found = shutil.which(str(argv[0]), path=env.get("PATH"))
            handler = fakes.get(os.path.realpath(found)) if found else None
            if handler:
                return _done(list(argv), handler([str(x) for x in argv[1:]], env), kw)
        return real(argv, *a, **kw)
    monkeypatch.setattr(subprocess, "run", run)


def _done(argv, result, kw):
    rc, out, err = result
    if kw.get("stderr") == subprocess.STDOUT:
        out, err = out + err, ""
    text = kw.get("text") or kw.get("universal_newlines") or kw.get("encoding") or kw.get("errors")
    captured = kw.get("capture_output")

    def keep(s, where, terminal):
        if captured or where == subprocess.PIPE:
            return s if text else s.encode()
        if where is None:   # not captured: a child would write to the terminal
            terminal.write(s)
        return None
    r = subprocess.CompletedProcess(argv, rc, keep(out, kw.get("stdout"), sys.stdout), keep(err, kw.get("stderr"), sys.stderr))
    if kw.get("check") and rc:
        raise subprocess.CalledProcessError(rc, argv, r.stdout, r.stderr)
    return r


_BUILDS = {}


def cached_build_ids(monkeypatch):
    """update.build_id_of hashes every file of the installed package (0.15 s); the package does not change while
    tests run, so each root is hashed once per test process. Tests that fake a new build still patch build_id."""
    from pl import update
    real = update.build_id_of

    def build_id_of(root):
        if str(root) not in _BUILDS:
            _BUILDS[str(root)] = real(root)
        return _BUILDS[str(root)]
    monkeypatch.setattr(update, "build_id_of", build_id_of)
