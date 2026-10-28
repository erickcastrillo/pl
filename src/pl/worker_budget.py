"""The machine-wide pytest worker budget every profile shares.

A run holds its workers as a lease file named by its pid under the machine folder; a lease counts only while that
pid lives, so a run that is killed or times out frees its workers without any cleanup step."""
import contextlib
import fcntl
import json
import os
import sys
import time

from pl import config as C
from pl import manager
from pl.util import now_iso

_cpus = os.cpu_count
_sleep = time.sleep
WAIT_POLL = 5
WAIT_REPEAT = 60


def max_workers() -> int:
    t = manager._machine_toml()
    if not C.validate_tests(t) and (value := t.get("tests", {}).get("max_workers")) is not None:
        return value
    return max(1, (_cpus() or 2) // 2)


def _alive(pid) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _leases_dir():
    return manager.machine_dir() / "test-workers"


@contextlib.contextmanager
def _locked():
    d = manager.machine_dir()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(d / "test-workers.lock", "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def _in_use():
    """Workers held by live leases; drops dead or unreadable ones. Call with the lock held."""
    total = 0
    try:
        paths = list(_leases_dir().glob("*.json"))
    except OSError:
        return 0
    for path in paths:
        try:
            lease = json.loads(path.read_text())
            pid, workers = lease["pid"], lease["workers"]
            alive = isinstance(pid, int) and isinstance(workers, int) and _alive(pid)
        except (OSError, ValueError, KeyError, TypeError):
            alive = False
        if alive:
            total += workers
        else:
            path.unlink(missing_ok=True)
    return total


def in_use() -> int:
    with _locked():
        return _in_use()


def allowed(ceiling=None) -> int:
    top = max_workers()
    return max(1, min(top - in_use(), ceiling or top))


def _try_lease(n):
    """Write this process's lease when n workers fit; the workers in use otherwise."""
    with _locked():
        used = _in_use()
        if used + n > max_workers():
            return used
        _leases_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(_leases_dir() / f"{os.getpid()}.json", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"pid": os.getpid(), "workers": n, "at": now_iso()}, f)
        return None


@contextlib.contextmanager
def hold(n, out=None):
    out = out or sys.stderr
    n = min(max(1, n), max_workers())
    last_said = None
    while (used := _try_lease(n)) is not None:
        if last_said is None or time.monotonic() - last_said >= WAIT_REPEAT:
            print(f"pl tests: waiting for test workers ({used}/{max_workers()} in use)", file=out, flush=True)
            last_said = time.monotonic()
        _sleep(WAIT_POLL)
    try:
        yield
    finally:
        (_leases_dir() / f"{os.getpid()}.json").unlink(missing_ok=True)
