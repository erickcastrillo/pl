"""Multipass sandbox VM lifecycle for isolated stage execution."""
import ipaddress
import json
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys

from pl import config as C
from pl import harnesses

DEFAULTS = {"enabled": False, "cpus": 2, "memory": "4G", "disk": "20G", "allow": []}
SIZE_RE = re.compile(r"[1-9][0-9]*[KMG]")
HOST_RE = re.compile(
    r"^(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)*"
    r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$"
)

SANDBOX_HOSTS = {
    "claude": ["api.anthropic.com"],
    "codex": ["api.openai.com"],
    "antigravity": ["generativelanguage.googleapis.com"],
}

MULTIPASS = "multipass"
INSTALL = ("pl sandbox needs Multipass (the multipass command): install it with "
           "sudo snap install multipass (Linux) or brew install --cask multipass (macOS); "
           "see https://canonical.com/multipass/install")


def is_host_or_cidr(val: object) -> bool:
    """True if val is a valid host name or IPv4/IPv6 CIDR/address."""
    if not isinstance(val, str) or not val.strip():
        return False
    val = val.strip()
    try:
        ipaddress.ip_network(val, strict=False)
        return True
    except ValueError:
        pass
    if val.replace(".", "").isdigit():
        return False
    return bool(HOST_RE.fullmatch(val)) and len(val) <= 253


def resolve_target(target: str, resolver=None) -> tuple[set[str], set[str]]:
    """Return (ipv4_set, ipv6_set) of strings for a host name or CIDR."""
    resolver = resolver or socket.getaddrinfo
    try:
        net = ipaddress.ip_network(target, strict=False)
        formatted = str(net) if net.num_addresses > 1 else str(net.network_address)
        if net.version == 4:
            return ({formatted}, set())
        else:
            return (set(), {formatted})
    except ValueError:
        pass

    v4, v6 = set(), set()
    try:
        infos = resolver(target, None)
        for family, _, _, _, sockaddr in infos:
            if family == socket.AF_INET:
                v4.add(sockaddr[0])
            elif family == socket.AF_INET6:
                v6.add(sockaddr[0])
    except (socket.gaierror, OSError):
        pass
    return (v4, v6)


def ruleset(harness: str | None = None,
            endpoint_port: int | None = None,
            allow: list[str] | None = None,
            include_code_host: bool | None = None,
            resolver=None) -> str:
    """Generate the constant nftables egress filtering ruleset."""
    if harness:
        harnesses_to_use = [harness]
    else:
        harnesses_to_use = [a.get("harness", "claude") for a in (C.ACCOUNTS or {}).values()] or ["claude"]

    harness_hosts = [h for hname in harnesses_to_use for h in SANDBOX_HOSTS.get(hname, [])]

    if include_code_host is None:
        include_code_host = bool(C.CODE_HOST and (C.CODE_HOST.get("owner") or C.CODE_HOST.get("repos")))
    code_hosts = ["github.com", "api.github.com"] if include_code_host else []

    if allow is None:
        allow = settings().get("allow") or []

    v4_addrs, v6_addrs = set(), set()
    for target in (*harness_hosts, *code_hosts, *allow):
        t_v4, t_v6 = resolve_target(target, resolver=resolver)
        v4_addrs.update(t_v4)
        v6_addrs.update(t_v6)

    port = endpoint_port if endpoint_port is not None else settings().get("endpoint_port")

    lines = [
        "flush ruleset",
        "",
        "table inet filter {",
        "    chain output {",
        "        type filter hook output priority 0; policy drop;",
        "        oifname \"lo\" accept",
        "        ct state established,related accept",
        "        udp dport 53 accept",
        "        tcp dport 53 accept",
    ]
    if port:
        lines.append(f"        tcp dport {port} accept")
    if v4_addrs:
        lines.append(f"        ip daddr {{ {', '.join(sorted(v4_addrs))} }} accept")
    if v6_addrs:
        lines.append(f"        ip6 daddr {{ {', '.join(sorted(v6_addrs))} }} accept")
    lines.extend([
        "    }",
        "}",
        "",
    ])
    return "\n".join(lines)


def apply_egress_rules(vm: str | None = None, **kw) -> None:
    """Apply the nftables allow list ruleset to the VM via stdin."""
    vm = vm or vm_name()
    text = ruleset(**kw)
    r = _run([MULTIPASS, "exec", vm, "--", "sudo", "nft", "-f", "-"], input=text)
    if r.returncode != 0:
        err = (r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout).strip() else "nftables failed"
        raise SystemExit(f"pl: {err}")


def up(vm: str | None = None, **kw) -> None:
    """Ensure the VM is running and apply the egress allow list ruleset."""
    vm = vm or vm_name()
    info = _info(vm)
    if info is None:
        raise SystemExit(f"pl: no sandbox VM for profile {C.PROFILE_NAME}: run pl sandbox create first")
    if info["state"] != "Running":
        r = _run([MULTIPASS, "start", vm])
        if r.returncode != 0:
            err = (r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout).strip() else "start failed"
            raise SystemExit(f"pl: {err}")
    apply_egress_rules(vm, **kw)


def _run(argv, **kw):
    """The module's single subprocess seam: run a multipass command without a shell."""
    capture = kw.pop("capture", True)
    return subprocess.run(argv, capture_output=capture, text=True, timeout=kw.pop("timeout", 120), **kw)


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


class SandboxCopyError(RuntimeError):
    pass


def _last_line(r, default: str) -> str:
    text = (r.stderr or r.stdout or "").strip()
    return text.splitlines()[-1] if text else default


def copy_in(src: Path | str, dest: str, vm: str | None = None) -> None:
    """Copy one host file into the VM at mode 0600: created empty first, content streamed on stdin."""
    vm = vm or vm_name()
    if harnesses.SECRET_RE.fullmatch(Path(src).name):
        raise SandboxCopyError(f"cannot copy {Path(src).name} into {vm}:{dest}: pl never copies a credential file")
    info = _info(vm)
    if info is None or info["state"] != "Running":
        raise SandboxCopyError(f"cannot copy into {vm}:{dest}: the VM is not running")
    r = _run([MULTIPASS, "exec", vm, "--", "sh", "-c",
              'umask 077 && rm -f -- "$1" && : > "$1" && chmod 600 -- "$1"', "sh", dest])
    if r.returncode != 0:
        raise SandboxCopyError(f"cannot copy into {vm}:{dest}: {_last_line(r, 'create failed')}")
    r = _run([MULTIPASS, "exec", vm, "--", "sh", "-c", 'cat > "$1"', "sh", dest], input=Path(src).read_text())
    if r.returncode != 0:
        _run([MULTIPASS, "exec", vm, "--", "rm", "-f", "--", dest])
        raise SandboxCopyError(f"cannot copy into {vm}:{dest}: {_last_line(r, 'write failed')}")


def _login(args) -> None:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise SystemExit("pl: sandbox login needs a real terminal: run it yourself in a terminal, "
                         "not piped or from the dispatcher")
    name = args.account
    if not name or name not in C.ACCOUNTS:
        raise SystemExit(f"pl: no account {name!r} in profile {C.PROFILE_NAME}; "
                         f"its accounts are {', '.join(C.ACCOUNTS) or 'none'}")
    h = harnesses.account_harness(name)
    argv = harnesses.login_argv(h)
    if not argv:
        raise SystemExit(f"pl: harness {h.name} has no known sign-in command to run in the sandbox")
    if not shutil.which(MULTIPASS):
        raise SystemExit("pl: " + INSTALL)
    vm = vm_name()
    info = _info(vm)
    if info is None:
        raise SystemExit(f"pl: no sandbox VM for profile {C.PROFILE_NAME}: run pl sandbox create first")
    if info["state"] != "Running":
        raise SystemExit(f"pl: sandbox VM {vm} is {info['state']}: run pl sandbox start first")
    if _run([MULTIPASS, "exec", vm, "--", "which", argv[0]]).returncode != 0:
        raise SystemExit(f"pl: {argv[0]} is not installed in {vm}: install it inside the VM "
                         f"(multipass shell {vm}), then run pl sandbox login {name} again")
    r = _run([MULTIPASS, "exec", vm, "--", *argv], capture=False, timeout=None)
    raise SystemExit(r.returncode)


def cmd_sandbox(args) -> None:
    if args.action == "login":
        _login(args)
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
        apply_egress_rules(vm)
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
        apply_egress_rules(vm)
        _status(vm)
    elif action == "up":
        vm = vm_name()
        up(vm)
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
