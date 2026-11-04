"""Long jobs: a file per running job in machine_dir()/jobs, so `pl jobs` can list them while they run."""
import contextlib
import json
import os
import sys
import time

from pl.manager import machine_dir

_current = None


def jobs_dir():
    return machine_dir() / "jobs"


def _write(path, job):
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(job, f)
    os.replace(tmp, path)


@contextlib.contextmanager
def track(name):
    global _current
    jobs_dir().mkdir(parents=True, exist_ok=True)
    job = {"id": f"{name}-{os.getpid()}", "name": name, "pid": os.getpid(), "argv": sys.argv, "started": time.time(), "progress": None}
    path = jobs_dir() / f"{job['id']}.json"
    _write(path, job)
    _current = (path, job)
    try:
        yield job
    finally:
        _current = None
        path.unlink(missing_ok=True)


def progress(line):
    print(line, flush=True)
    if _current:
        path, job = _current
        job["progress"] = line
        _write(path, job)


def result(text):
    print(f"result: {text}", flush=True)


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def live():
    """Jobs whose process still runs; the file of one that died is dropped."""
    out = []
    for path in sorted(jobs_dir().glob("*.json")):
        try:
            job = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if _alive(job.get("pid", 0)):
            out.append(job)
        else:
            path.unlink(missing_ok=True)
    return out


def cmd_jobs(a):
    """pl jobs [--json]: the long jobs running now."""
    items = live()
    if getattr(a, "json", False):
        from pl import jsonout
        return jsonout.emit({"jobs": items})
    for j in items:
        print(f"{j['name']:<10} pid {j['pid']:<8} {j['progress'] or ''}")
    if not items:
        print("no jobs running")
