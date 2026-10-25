"""Multipass layer for pl e2e: launch a throwaway VM, move files in and out, run steps and services, delete it.

Every command is an argv list run through _run (tests fake it). Inside the VM, the bash -c strings are constants;
script and log paths, hosts and ports travel as positional arguments, never spliced into a shell string."""
import json
import os
import re
import subprocess
import time

TIMEOUT = 124          # _run's return code when a command ran out of time (as timeout(1) does)
NOT_FOUND = 127        # _run's return code when the program could not be started
STEP_SH = 'set -o pipefail; bash -euo pipefail "$1" > "$2" 2>&1'
SERVICE_SH = 'setsid -f nohup bash -euo pipefail "$1" > "$2" 2>&1 < /dev/null'
TCP_SH = 'exec 3<>/dev/tcp/"$1"/"$2"'
PREFIX = "pl-e2e-"
_clock, _sleep = time.monotonic, time.sleep


class VMError(Exception):
    """A Multipass command failed, or a path or target cannot be used."""


def _run(argv, timeout=None):
    """The one subprocess seam of this module (tests fake it): (return code, stdout and stderr together)."""
    try:
        r = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return TIMEOUT, f"{argv[0]} {argv[1] if len(argv) > 1 else ''} timed out after {timeout}s"
    except OSError as e:
        return NOT_FOUND, f"could not run {argv[0]}: {e}"
    return r.returncode, r.stdout or ""


def _check(argv, timeout, what):
    code, out = _run(argv, timeout)
    if code != 0:
        raise VMError(f"{what} failed (exit {code}): {out.strip()[-2000:]}")
    return out


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def vm_name(repo_name, stamp):
    """pl-e2e-<repo slug>-<stamp>: letters, digits and hyphens, starts with a letter, at most 63 characters."""
    stamp = _slug(stamp) or "0"
    room = 63 - len(PREFIX) - len(stamp) - 1
    slug = _slug(repo_name)[:room].strip("-") or "app"
    return f"{PREFIX}{slug}-{stamp}"


def cloud_init(packages):
    """#cloud-config text that updates apt and installs packages (JSON strings are valid YAML scalars)."""
    lines = ["#cloud-config", "package_update: true"]
    lines += ["packages:", *(f"  - {json.dumps(str(p))}" for p in packages)] if packages else ["packages: []"]
    return "\n".join(lines) + "\n"


def check_host_path(path):
    """The absolute host path, or VMError when Multipass (a snap) cannot reach it: under /tmp or in a hidden folder."""
    p = os.path.abspath(os.path.expanduser(str(path)))
    parts = p.split(os.sep)
    if p == "/tmp" or p.startswith("/tmp/") or any(x.startswith(".") for x in parts if x):
        raise VMError(f"{p}: Multipass runs as a snap and cannot read /tmp or hidden folders on the host; "
                      "use a plain folder in your home, for example --out ~/pl-e2e/<repo>")
    return p


def launch(name, image, cpus, memory, disk, cloud_init_path, timeout=900):
    """Start a new VM; VMError when it does not come up."""
    path = check_host_path(cloud_init_path)
    _check(["multipass", "launch", str(image), "--name", name, "--cpus", str(cpus), "--memory", str(memory),
            "--disk", str(disk), "--cloud-init", path, "--timeout", str(timeout)], timeout + 60, f"launching {name}")


def transfer_in(name, host_path, vm_path, timeout=600):
    _check(["multipass", "transfer", check_host_path(host_path), f"{name}:{vm_path}"], timeout,
           f"copying {host_path} into {name}")


def transfer_out(name, vm_path, host_path, timeout=600):
    _check(["multipass", "transfer", f"{name}:{vm_path}", check_host_path(host_path)], timeout,
           f"copying {vm_path} out of {name}")


def _exec(name, args, cwd=None):
    return ["multipass", "exec", name, *(["--working-directory", cwd] if cwd else []), "--", *args]


def exec_step(name, script_vm_path, log_vm_path, cwd, timeout):
    """Run a step script in the VM with its output kept in log_vm_path; the exit code (TIMEOUT when it ran out of time)."""
    return _run(_exec(name, ["bash", "-c", STEP_SH, "_", script_vm_path, log_vm_path], cwd), timeout)[0]


def start_service(name, script_vm_path, log_vm_path, cwd, timeout=60):
    """Start a long-running script detached in the VM (setsid, nohup), its output in log_vm_path; the exit code."""
    return _run(_exec(name, ["bash", "-c", SERVICE_SH, "_", script_vm_path, log_vm_path], cwd), timeout)[0]


def _probe(ready):
    """The in-VM command that answers 0 when ready is up: http(s)://..., tcp:PORT or tcp:HOST:PORT."""
    if ready.startswith(("http://", "https://")):
        return ["curl", "-fsS", "-o", "/dev/null", "--max-time", "5", ready]
    m = re.fullmatch(r"tcp:(?:([A-Za-z0-9.\-]+):)?(\d{1,5})", ready)
    if not m:
        raise VMError(f"ready {ready!r}: must be http://..., https://..., tcp:PORT or tcp:HOST:PORT")
    return ["bash", "-c", TCP_SH, "_", m.group(1) or "localhost", m.group(2)]


def wait_ready(name, ready, timeout, interval=2, clock=None, sleep=None):
    """Poll ready from inside the VM until it answers (True) or timeout seconds pass (False)."""
    clock, sleep = clock or _clock, sleep or _sleep
    argv = _exec(name, _probe(ready))
    deadline = clock() + timeout
    while True:
        if _run(argv, 15)[0] == 0:
            return True
        if clock() + interval > deadline:
            return False
        sleep(interval)


def exists(name):
    return _run(["multipass", "info", name], 60)[0] == 0


def delete(name):
    """Delete and purge the VM; quiet when it is already gone, VMError when it is still there."""
    code, out = _run(["multipass", "delete", "--purge", name], 300)
    if code != 0 and exists(name):
        raise VMError(f"deleting {name} failed (exit {code}): {out.strip()[-2000:]}")
