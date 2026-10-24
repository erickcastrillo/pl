"""Multipass sandbox VM lifecycle for isolated stage execution."""
import json
from pathlib import Path
import re
import shutil
import subprocess

from pl import config as C

DEFAULTS = {"enabled": False, "cpus": 2, "memory": "4G", "disk": "20G"}
SIZE_RE = re.compile(r"[1-9][0-9]*[KMG]")

MULTIPASS = "multipass"
INSTALL = ("pl sandbox needs Multipass (the multipass command): install it with "
           "sudo snap install multipass (Linux) or brew install --cask multipass (macOS); "
           "see https://canonical.com/multipass/install")


def _run(argv, **kw):
    """The module's single subprocess seam: run a multipass command without a shell."""
    return subprocess.run(argv, capture_output=True, text=True, timeout=kw.pop("timeout", 120), **kw)


def settings() -> dict:
    """[sandbox] over the defaults."""
    return {**DEFAULTS, **(C.SANDBOX or {})}


def vm_name() -> str:
    """The sandbox VM name for the current profile."""
    return f"pl-sbx-{C.PROFILE_NAME}"


def bridge_address() -> str:
    """The Multipass bridge IP address on the host (never 0.0.0.0 or LAN IP)."""
    addr = settings().get("bridge_address")
    if not addr:
        try:
            r = subprocess.run(["ip", "-brief", "addr"], capture_output=True, text=True, timeout=5)
            for line in (r.stdout.splitlines() if r.returncode == 0 else []):
                p = line.split()
                if len(p) >= 3 and p[0].startswith("mp") and not p[2].startswith("fe80:"):
                    addr = p[2].split("/")[0]
                    break
        except Exception:
            pass
    addr = addr or "10.0.0.1"
    if addr in ("0.0.0.0", "", "::"):
        raise ValueError("Invalid bridge address: refusing 0.0.0.0")
    return addr


def transfer_in(vm: str, host_path: Path | str, vm_path: str) -> None:
    r = _run([MULTIPASS, "transfer", str(host_path), f"{vm}:{vm_path}"])
    if r.returncode != 0:
        raise SystemExit(f"pl: {(r.stderr or r.stdout).strip() or 'transfer failed'}")


def transfer_out(vm: str, vm_path: str, host_path: Path | str) -> None:
    r = _run([MULTIPASS, "transfer", f"{vm}:{vm_path}", str(host_path)])
    if r.returncode != 0:
        raise SystemExit(f"pl: {(r.stderr or r.stdout).strip() or 'transfer failed'}")


def _forbidden_folders() -> list[Path]:
    home = Path.home().resolve()
    dirs = [
        home / ".ssh",
        home / ".codex",
        home / ".gemini",
        home / ".config" / "gh",
        home / ".claude",
    ]
    try:
        dirs.extend(p.resolve() for p in home.glob(".pl-*"))
        dirs.extend(p.resolve() for p in home.glob(".claude*"))
    except OSError:
        pass
    for p in (getattr(C, "PROFILES", {}) or {}).values():
        if p:
            dirs.append(Path(p).expanduser().resolve())
    return [d.resolve() for d in dirs]


def mounts() -> list[Path]:
    """The two host folders mounted into the sandbox: work_dir and worktrees_dir."""
    if not C.WORK_DIR or not C.WORKTREES_DIR:
        raise SystemExit("pl: profile has no work_dir or worktrees_dir")
    paths = [C.WORK_DIR.resolve(), C.WORKTREES_DIR.resolve()]
    home = Path.home().resolve()
    forbidden = _forbidden_folders()
    for p in paths:
        if home.is_relative_to(p):
            raise SystemExit(f"pl: {p} cannot be mounted: set [paths] work_dir to a project folder")
        for f in forbidden:
            if p == f or p.is_relative_to(f) or f.is_relative_to(p):
                raise SystemExit(f"pl: {p} cannot be mounted: set [paths] work_dir to a project folder")
        if any(part.startswith(".pl-") or part.startswith(".claude") for part in p.parts):
            raise SystemExit(f"pl: {p} cannot be mounted: set [paths] work_dir to a project folder")
    return paths


def _info(name: str | None = None) -> dict | None:
    """VM state and mounts from multipass info <name> --format json, or None if the VM does not exist."""
    name = name or vm_name()
    r = _run([MULTIPASS, "info", name, "--format", "json"])
    if r.returncode != 0:
        return None
    try:
        data = json.loads(r.stdout)
    except Exception as e:
        raise SystemExit(f"pl: multipass info returned bad JSON: {e}")
    info_map = data.get("info", {})
    entry = info_map.get(name) if isinstance(info_map, dict) else data.get(name)
    if not entry or not isinstance(entry, dict):
        return None
    mounts_dict = {}
    for tgt, src_val in (entry.get("mounts") or {}).items():
        if isinstance(src_val, dict):
            mounts_dict[tgt] = src_val.get("source_path", tgt)
        else:
            mounts_dict[tgt] = str(src_val)
    return {"state": entry.get("state", "Unknown"), "mounts": mounts_dict}


def _status(vm: str) -> None:
    info = _info(vm)
    if info is None:
        raise SystemExit(f"pl: no sandbox VM for profile {C.PROFILE_NAME}: run pl sandbox create first")
    print(f"{vm}: {info['state']}")
    for target, source in (info.get("mounts") or {}).items():
        print(f"  {source} -> {target}")


def cmd_sandbox(args) -> None:
    if not shutil.which(MULTIPASS):
        raise SystemExit("pl: " + INSTALL)
    action = args.action
    if action == "create":
        ms = mounts()
        vm = vm_name()
        if _info(vm) is not None:
            print(f"sandbox VM {vm} already exists")
            return
        errs = C._sandbox_errors(C.SANDBOX)
        if errs:
            raise SystemExit("pl: " + "; ".join(errs))
        C.WORKTREES_DIR.mkdir(parents=True, exist_ok=True)
        s = settings()
        cmd = [MULTIPASS, "launch", "lts", "--name", vm, "--cpus", str(s["cpus"]),
               "--memory", str(s["memory"]), "--disk", str(s["disk"])]
        for m in ms:
            cmd.extend(["--mount", f"{m}:{m}"])
        r = _run(cmd, timeout=900)
        if r.returncode != 0:
            err = (r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout).strip() else "launch failed"
            raise SystemExit(f"pl: {err}")
        _status(vm)
    elif action == "status":
        _status(vm_name())
    elif action == "start":
        vm = vm_name()
        if _info(vm) is None:
            raise SystemExit(f"pl: no sandbox VM for profile {C.PROFILE_NAME}: run pl sandbox create first")
        r = _run([MULTIPASS, "start", vm])
        if r.returncode != 0:
            err = (r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout).strip() else "start failed"
            raise SystemExit(f"pl: {err}")
        _status(vm)
    elif action == "stop":
        vm = vm_name()
        if _info(vm) is None:
            raise SystemExit(f"pl: no sandbox VM for profile {C.PROFILE_NAME}: run pl sandbox create first")
        r = _run([MULTIPASS, "stop", vm])
        if r.returncode != 0:
            err = (r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout).strip() else "stop failed"
            raise SystemExit(f"pl: {err}")
        _status(vm)
    elif action == "destroy":
        vm = vm_name()
        if not getattr(args, "yes", False):
            raise SystemExit(f"pl: this deletes sandbox VM {vm} for good; add --yes")
        if _info(vm) is None:
            raise SystemExit(f"pl: no sandbox VM for profile {C.PROFILE_NAME}: run pl sandbox create first")
        r = _run([MULTIPASS, "delete", "--purge", vm])
        if r.returncode != 0:
            err = (r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout).strip() else "delete failed"
            raise SystemExit(f"pl: {err}")
        print(f"sandbox VM {vm} deleted")
