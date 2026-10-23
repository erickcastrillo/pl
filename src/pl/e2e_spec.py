"""pl e2e's spec: load and check a repo's .pl/e2e.yaml into plain data, with ${VAR} env values read from the host."""
import posixpath
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

import yaml

NAME_RE = re.compile(r"[A-Za-z0-9._-]+")              # step and service names become log file names
VAR_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
PKG_RE = re.compile(r"[a-z0-9][a-z0-9.+:=~-]*")
SIZE_RE = re.compile(r"[1-9][0-9]*[MG]")
TCP_RE = re.compile(r"tcp:(?:([A-Za-z0-9.-]+):)?([0-9]{1,5})")
VAR_RE = re.compile(r"\$\$|\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
TOP = {"version", "vm", "timeout", "packages", "env", "setup", "services", "tests", "artifacts"}


class SpecError(ValueError):
    """Every problem found in a spec, one per line, each starting with its key path."""

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("invalid .pl/e2e.yaml:\n" + "\n".join(f"  {p}" for p in self.problems))


@dataclass
class VM:
    image: str = "24.04"
    cpus: int = 4
    memory: str = "8G"
    disk: str = "30G"


@dataclass
class Step:
    name: str
    run: str


@dataclass
class Service:
    name: str
    run: str
    ready: str
    timeout: int = 300


@dataclass
class Tests:
    run: str
    dir: str = "."
    report: str | None = None
    timeout: int = 1800


@dataclass
class Spec:
    tests: Tests
    version: int = 1
    vm: VM = field(default_factory=VM)
    timeout: int = 3600
    packages: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    setup: list[Step] = field(default_factory=list)
    services: list[Service] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    secrets: list[str] = field(default_factory=list)   # resolved ${VAR} values, for mask()


class _Checker:
    """Reads values out of the parsed YAML, noting each problem with its key path instead of stopping."""

    def __init__(self):
        self.problems = []

    def bad(self, path, msg):
        self.problems.append(f"{path}: {msg}")

    def mapping(self, d, path, allowed):
        """d as a dict (or None when missing or not a mapping), with unknown keys noted."""
        if d is None:
            return None
        if not isinstance(d, dict):
            return self.bad(path, "must be a mapping")
        for k in d:
            if k not in allowed:
                self.bad(f"{path}.{k}" if path else str(k), "unknown key")
        return d

    def list(self, v, path):
        if v is None:
            return []
        if not isinstance(v, list):
            self.bad(path, "must be a list")
            return []
        return v

    def string(self, d, key, path, default=None, required=False):
        v = d.get(key)
        if v is None or (required and isinstance(v, str) and not v.strip()):
            if required:
                self.bad(path, "required")
            return default
        if not isinstance(v, str):
            self.bad(path, "must be a string")
            return default
        return v

    def positive(self, d, key, path, default):
        v = d.get(key, default)
        if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
            self.bad(path, "must be a positive integer")
            return default
        return v

    def size(self, d, key, path, default):
        v = d.get(key, default)
        if not isinstance(v, str) or not SIZE_RE.fullmatch(v):
            self.bad(path, "must look like 8G or 512M")
            return default
        return v

    def relpath(self, v, path):
        if not isinstance(v, str) or not v or v.startswith("/") or ".." in posixpath.normpath(v).split("/"):
            self.bad(path, "must be a relative path inside the checkout")
        return v

    def ready(self, v, path):
        if v is None:
            return self.bad(path, "required")
        if not isinstance(v, str):
            return self.bad(path, "must be a string")
        if v.startswith(("http://", "https://")):
            return v
        if not v.startswith("tcp:"):
            return self.bad(path, "must start with http://, https:// or tcp:")
        m = TCP_RE.fullmatch(v)
        if not m or not 0 < int(m.group(2)) < 65536:
            return self.bad(path, "must be tcp:PORT or tcp:HOST:PORT")
        return v


def _resolve(value, path, env, ck, secrets):
    """value with each ${VAR} read from env and $$ turned into $; an unset VAR is a problem (its value never shown)."""
    def sub(m):
        if m.group(0) == "$$":
            return "$"
        name = m.group(1)
        if name not in env:
            ck.bad(path, f"${{{name}}} is not set on the host")
            return ""
        if env[name] and env[name] not in secrets:
            secrets.append(env[name])
        return env[name]
    return VAR_RE.sub(sub, value)


def _steps(ck, items, kind, names, service=False):
    keys = {"name", "run", "ready", "timeout"} if service else {"name", "run"}
    out = []
    for i, item in enumerate(ck.list(items, kind)):
        p = f"{kind}[{i}]"
        if not isinstance(item, dict):
            ck.bad(p, "must be a mapping")
            continue
        ck.mapping(item, p, keys)
        name = ck.string(item, "name", f"{p}.name", required=True)
        if name is not None:
            if not NAME_RE.fullmatch(name):
                ck.bad(f"{p}.name", "only letters, digits, '.', '_' and '-'")
            elif name in names:
                ck.bad(f"{p}.name", f"duplicate name {name!r}")
            names.add(name)
        run = ck.string(item, "run", f"{p}.run", required=True)
        if service:
            ready = ck.ready(item.get("ready"), f"{p}.ready")
            out.append(Service(name, run, ready, ck.positive(item, "timeout", f"{p}.timeout", 300)))
        else:
            out.append(Step(name, run))
    return out


def load(text: str, env: Mapping[str, str]) -> Spec:
    """Parse .pl/e2e.yaml text (yaml.safe_load only) into a Spec; raise SpecError listing every problem."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise SpecError([f"not valid YAML: {e}"]) from None
    if not isinstance(data, dict):
        raise SpecError(["the file must be a YAML mapping"])
    ck = _Checker()
    if "version" not in data:
        ck.bad("version", "required")
    elif type(data["version"]) is not int or data["version"] != 1:
        ck.bad("version", "must be 1")
    ck.mapping(data, "", TOP)
    vm = VM()
    if (v := ck.mapping(data.get("vm"), "vm", {"image", "cpus", "memory", "disk"})) is not None:
        vm = VM(ck.string(v, "image", "vm.image", vm.image), ck.positive(v, "cpus", "vm.cpus", vm.cpus),
                ck.size(v, "memory", "vm.memory", vm.memory), ck.size(v, "disk", "vm.disk", vm.disk))
    timeout = ck.positive(data, "timeout", "timeout", 3600)
    packages = []
    for i, p in enumerate(ck.list(data.get("packages"), "packages")):
        if not isinstance(p, str) or not PKG_RE.fullmatch(p):
            ck.bad(f"packages[{i}]", "not a valid apt package name")
        else:
            packages.append(p)
    resolved, secrets = {}, []
    env_map = {} if data.get("env") is None else data["env"]
    if not isinstance(env_map, dict):
        ck.bad("env", "must be a mapping")
        env_map = {}
    for k, v in env_map.items():
        p = f"env.{k}"
        if not isinstance(k, str) or not VAR_NAME_RE.fullmatch(k):
            ck.bad(p, "not a valid variable name")
        elif not isinstance(v, str):
            ck.bad(p, "must be a string")
        else:
            resolved[k] = _resolve(v, p, env, ck, secrets)
    names = set()
    setup = _steps(ck, data.get("setup"), "setup", names)
    services = _steps(ck, data.get("services"), "services", names, service=True)
    tests = Tests(run="")
    if "tests" not in data or data["tests"] is None:
        ck.bad("tests", "required")
    elif (t := ck.mapping(data["tests"], "tests", {"dir", "run", "report", "timeout"})) is not None:
        tests = Tests(ck.string(t, "run", "tests.run", "", required=True),
                      ck.relpath(ck.string(t, "dir", "tests.dir", "."), "tests.dir"),
                      None, ck.positive(t, "timeout", "tests.timeout", 1800))
        if (r := ck.string(t, "report", "tests.report")) is not None:
            tests.report = ck.relpath(r, "tests.report")
    artifacts = [ck.relpath(a, f"artifacts[{i}]") for i, a in enumerate(ck.list(data.get("artifacts"), "artifacts"))]
    if ck.problems:
        raise SpecError(ck.problems)
    return Spec(tests, 1, vm, timeout, packages, resolved, setup, services, artifacts, secrets)


def mask(text: str, secrets) -> str:
    """text with every secret value replaced by ***, longest first so a secret inside another is not half-shown."""
    for s in sorted((s for s in secrets if s), key=len, reverse=True):
        text = text.replace(s, "***")
    return text
