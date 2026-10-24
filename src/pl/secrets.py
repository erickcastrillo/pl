"""Connector credentials: the OS keychain when there is one, else 0600 files under <machine_dir>/secrets."""
import base64
import json
import os
import re
import shutil
import subprocess
import sys

SERVICE = "pl-connector"
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
SECRET_KEYS = {"access_token", "refresh_token", "id_token", "client_secret", "api_key"}


def _run(argv, input=None):
    return subprocess.run(argv, input=input, capture_output=True, text=True)


def _tool():
    if os.environ.get("PL_KEYCHAIN") == "off":
        return None
    tool = "security" if sys.platform == "darwin" else "secret-tool"
    return tool if shutil.which(tool) else None


def backend():
    return "keychain" if _tool() else "file"


def _check(name):
    if not NAME_RE.match(name or ""):
        raise ValueError(f"not a plain connector name: {name!r}")


def _dir():
    from pl.manager import machine_dir
    d = machine_dir() / "secrets"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    d.chmod(0o700)
    return d


def _file(name):
    p = _dir() / f"{name}.json"
    if p.is_symlink():
        raise SystemExit(f"pl: {p} is a symlink; remove it")
    return p


def _encode(data):
    return base64.b64encode(json.dumps(data).encode()).decode()


def _decode(blob):
    return json.loads(base64.b64decode(blob.strip()))


def _mask_values(data):
    from pl.trackers import mcp
    for key, val in data.items():
        if key in SECRET_KEYS and isinstance(val, str) and val:
            mcp._secrets.add(val)
        elif isinstance(val, dict):
            _mask_values(val)


def _keychain_put(tool, name, blob):
    if tool == "security":
        line = f"add-generic-password -U -s {SERVICE} -a {name} -w {blob}\n"
        r = _run(["security", "-i"], input=line)
    else:
        r = _run(["secret-tool", "store", f"--label=pl connector {name}", "service", SERVICE, "account", name], input=blob)
    if r.returncode:
        raise SystemExit(f"pl: the keychain refused the credentials for {name}")


def _keychain_get(tool, name):
    if tool == "security":
        r = _run(["security", "find-generic-password", "-s", SERVICE, "-a", name, "-w"])
    else:
        r = _run(["secret-tool", "lookup", "service", SERVICE, "account", name])
    return r.stdout if r.returncode == 0 and r.stdout.strip() else None


def _keychain_delete(tool, name):
    if tool == "security":
        _run(["security", "delete-generic-password", "-s", SERVICE, "-a", name])
    else:
        _run(["secret-tool", "clear", "service", SERVICE, "account", name])


def _file_put(name, blob):
    d, path = _dir(), _file(name)
    tmp = d / f".{name}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(blob)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _file_get(name):
    path = _file(name)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(fd) as f:
        return f.read()


def put(name, data):
    _check(name)
    blob, tool = _encode(data), _tool()
    if tool:
        _keychain_put(tool, name, blob)
    else:
        _file_put(name, blob)
    _mask_values(data)


def get(name):
    _check(name)
    tool = _tool()
    blob = _keychain_get(tool, name) if tool else _file_get(name)
    if not blob:
        return None
    data = _decode(blob)
    _mask_values(data)
    return data


def delete(name):
    _check(name)
    if tool := _tool():
        _keychain_delete(tool, name)
    path = _file(name)
    path.unlink(missing_ok=True)
