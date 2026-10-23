"""pl e2e: run a repo's Playwright suite in a throwaway Multipass VM, set up from its .pl/e2e.yaml (pl e2e --help).

git runs through _run (tests fake it); Multipass only through pl.e2e_vm. Host files live in a plain folder
(default ~/pl-e2e/<repo>/<timestamp>/), since the Multipass snap cannot read /tmp or hidden folders."""
import argparse
import dataclasses
import json
import os
import posixpath
import shlex
import subprocess
import sys
import tarfile
import time
from datetime import datetime
from pathlib import Path

from pl import e2e_report as R
from pl import e2e_spec as S
from pl import e2e_vm as V
from pl.util import now_iso

HOME_VM = "/home/ubuntu"
APP = f"{HOME_VM}/app"
WORK = f"{HOME_VM}/.pl-e2e"
BUNDLE, BOOT = f"{HOME_VM}/pl-e2e-bundle.tar", f"{HOME_VM}/pl-e2e-boot.sh"
OUT_TARS = {"artifacts": f"{HOME_VM}/pl-e2e-artifacts.tar.gz", "logs": f"{HOME_VM}/pl-e2e-logs.tar.gz"}
BOOT_SH = f"""# pl e2e: wait for cloud-init (2 = finished with warnings), unpack the stage and the app
cloud-init status --wait > /dev/null || [ $? -eq 2 ]
mkdir -p {WORK}/logs {APP}
tar -xf {BUNDLE} -C {WORK}
rm -f {BUNDLE}
chmod 600 {WORK}/env
tar -xzf {WORK}/app.tar.gz -C {APP}
"""
TAIL = 40
COLLECT_TIMEOUT = 600
MASK_MAX = 64 * 1024 * 1024        # larger files (videos) are not read for masking
_clock = time.monotonic


class Stop(Exception):
    """A problem found before any VM exists: printed, exit 2."""


def _run(argv, timeout=300):
    """The one git seam of this module (tests fake it): (return code, stdout, stderr)."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)
    return r.returncode, r.stdout, r.stderr


def _git(repo, *args):
    code, out, err = _run(["git", "-C", str(repo), *args])
    if code:
        raise Stop(f"pl e2e: git {args[0]} failed in {repo}: {(err or out).strip()}")
    return out.strip()


def _script(run, *exports):
    lines = ["#!/bin/bash", f"source {WORK}/env", *(f"export {k}={shlex.quote(v)}" for k, v in exports), run]
    return "\n".join(lines) + "\n"


def _collect_sh(paths):
    """Tar the existing artifact paths (app-relative) and the logs; missing paths are left out, never an error."""
    quoted = " ".join(shlex.quote(p) for p in paths)
    return f"""mkdir -p {WORK}/logs
cp {HOME_VM}/pl-e2e-boot.log {WORK}/logs/ 2> /dev/null || true
cd {APP} 2> /dev/null || cd {HOME_VM}
: > {WORK}/collect.list
for p in {quoted or '""'}; do if [ -n "$p" ] && [ -e "$p" ]; then printf '%s\\0' "$p" >> {WORK}/collect.list; fi; done
tar --null -czf {OUT_TARS['artifacts']} -T {WORK}/collect.list
tar -czf {OUT_TARS['logs']} -C {WORK} logs
"""


def _private(path, mode="w"):
    """A new file only its owner can read, made so before anything is written (it holds secrets)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    return os.fdopen(fd, mode)


def _bundle(stage, names):
    """stage/bundle.tar (private: it holds the env file) with the app archive, the env file and the scripts."""
    with _private(stage / "bundle.tar", "wb") as f, tarfile.open(fileobj=f, mode="w") as t:
        for n in names:
            t.add(stage / n, arcname=n)


def _prepare(a, env):
    repo = Path(_git(a.repo or os.getcwd(), "rev-parse", "--show-toplevel"))
    sha = _git(repo, "rev-parse", "--verify", f"{a.ref}^{{commit}}")
    code, text, _ = _run(["git", "-C", str(repo), "show", f"{sha}:.pl/e2e.yaml"])
    if code:
        raise Stop(f"pl e2e: {repo.name} has no .pl/e2e.yaml at {a.ref}; add one and commit it (pl e2e --help)")
    try:
        spec = S.load(text, env)
    except S.SpecError as e:
        raise Stop(str(e)) from None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        out = Path(V.check_host_path(a.out or Path.home() / "pl-e2e" / repo.name / stamp))
    except V.VMError as e:
        raise Stop(f"pl e2e: {e}") from None
    stage = out / "stage"
    stage.mkdir(parents=True, exist_ok=True)
    _git(repo, "archive", "--format=tar.gz", "-o", str(stage / "app.tar.gz"), sha)
    (stage / "cloud-init.yaml").write_text(V.cloud_init(spec.packages))
    with _private(stage / "env") as f:
        f.write("".join(f"export {k}={shlex.quote(v)}\n" for k, v in spec.env.items()))
    t = spec.tests
    report = posixpath.normpath(posixpath.join(t.dir, t.report)) if t.report else None
    scripts = {f"setup-{i}.sh": _script(s.run) for i, s in enumerate(spec.setup, 1)}
    scripts |= {f"service-{s.name}.sh": _script(s.run) for s in spec.services}
    scripts["tests.sh"] = _script(t.run, *([("PLAYWRIGHT_JSON_OUTPUT_NAME", f"{APP}/{report}")] if report else []))
    scripts["collect.sh"] = _collect_sh(spec.artifacts + ([report] if report else []))
    for n, text in scripts.items():
        (stage / n).write_text(text)
    (stage / "pl-e2e-boot.sh").write_text(BOOT_SH)
    _bundle(stage, ["app.tar.gz", "env", *scripts])
    return {"repo": repo, "ref": a.ref, "sha": sha, "spec": spec, "out": out, "stage": stage, "report": report,
            "vm": V.vm_name(repo.name, stamp), "keep": a.keep_vm}


def _plan(spec):
    """Every step the run may take, all skipped until they run: (step dict, what runs it)."""
    def step(kind, name, log):
        return {"kind": kind, "name": name, "status": "skipped", "seconds": None, "log": log, "tail": None}
    steps = [(step("setup", "start-vm", None), ("vm",))]
    steps += [(step("setup", s.name, f"logs/setup-{i}-{s.name}.log"), ("setup", i, s.name))
              for i, s in enumerate(spec.setup, 1)]
    steps += [(step("service", s.name, f"logs/service-{s.name}.log"), ("service", s)) for s in spec.services]
    return steps + [(step("tests", "tests", "logs/tests.log"), ("tests",))]


def _do(ctx, what, left):
    """Run one step; True when it went well, else False or the step status ("timeout")."""
    name, spec = ctx["vm"], ctx["spec"]
    if what[0] == "vm":
        ctx["launched"] = True
        print(f"launching {name} ({spec.vm.image}, {spec.vm.cpus} cpus, {spec.vm.memory}, {spec.vm.disk})", flush=True)
        V.launch(name, spec.vm.image, spec.vm.cpus, spec.vm.memory, spec.vm.disk,
                 str(ctx["stage"] / "cloud-init.yaml"), timeout=int(min(900, left())))
        ctx["up"] = True
        V.transfer_in(name, str(ctx["stage"] / "bundle.tar"), BUNDLE)
        V.transfer_in(name, str(ctx["stage"] / "pl-e2e-boot.sh"), BOOT)
        code = V.exec_step(name, BOOT, f"{HOME_VM}/pl-e2e-boot.log", HOME_VM, left())
        if code:
            raise V.VMError(f"preparing the VM failed (exit {code}); see logs/pl-e2e-boot.log")
        return True
    if what[0] == "service":
        s = what[1]
        if V.start_service(name, f"{WORK}/service-{s.name}.sh", f"{WORK}/logs/service-{s.name}.log", APP):
            return False
        return V.wait_ready(name, s.ready, min(s.timeout, left())) or "timeout"
    if what[0] == "setup":
        code = V.exec_step(name, f"{WORK}/setup-{what[1]}.sh", f"{WORK}/logs/setup-{what[1]}-{what[2]}.log", APP, left())
    else:
        cwd = posixpath.normpath(posixpath.join(APP, spec.tests.dir))
        code = V.exec_step(name, f"{WORK}/tests.sh", f"{WORK}/logs/tests.log", cwd, min(spec.tests.timeout, left()))
    return True if code == 0 else "timeout" if code == V.TIMEOUT else False


def _drive(ctx, steps):
    deadline = _clock() + ctx["spec"].timeout

    def left():
        return max(1, int(deadline - _clock()))
    for step, what in steps:
        step["status"], t0 = "running", _clock()
        if _clock() >= deadline:
            step["status"], step["note"] = "timeout", f"the run's timeout ({ctx['spec'].timeout}s) ran out"
        else:
            got = _do(ctx, what, left)
            step["status"] = "ok" if got is True else got or "failed"
        step["seconds"] = round(_clock() - t0, 1)
        print(f"{step['kind']} {step['name']}: {step['status']} ({step['seconds']:.1f}s)", flush=True)
        if step["status"] != "ok":
            return ("tests_failed", 1) if step["kind"] == "tests" else ("setup_failed", 2)
    return "passed", 0


def _collect(ctx):
    name, out = ctx["vm"], ctx["out"]
    if V.exec_step(name, f"{WORK}/collect.sh", f"{HOME_VM}/pl-e2e-collect.log", HOME_VM, COLLECT_TIMEOUT):
        print("pl e2e: collecting artifacts in the VM failed; copying what is there", file=sys.stderr)
    for kind, vm_path in OUT_TARS.items():
        host = ctx["stage"] / os.path.basename(vm_path)
        V.transfer_out(name, vm_path, str(host))
        with tarfile.open(host) as t:
            t.extractall(out / kind if kind == "artifacts" else out, filter="data")
        host.unlink()


def _mask_tree(root, secrets):
    for p in (root.rglob("*") if root.is_dir() and secrets else []):
        try:
            if not p.is_file() or p.is_symlink() or p.stat().st_size > MASK_MAX:
                continue
            text = p.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if any(s in text for s in secrets):
            p.write_text(S.mask(text, secrets), encoding="utf-8")


def _masked(v, secrets):
    if isinstance(v, str):
        return S.mask(v, secrets)
    if isinstance(v, dict):
        return {k: _masked(x, secrets) for k, x in v.items()}
    return [_masked(x, secrets) for x in v] if isinstance(v, list) else v


def _tail(out, step):
    try:
        lines = (out / step["log"]).read_text(errors="replace").splitlines()[-TAIL:] if step["log"] else []
    except OSError:
        lines = []
    return lines + ([f"pl e2e: {step.pop('note')}"] if step.get("note") else [])


def _finish(ctx, steps, outcome, code, started):
    out, spec, name = ctx["out"], ctx["spec"], ctx["vm"]
    if ctx.get("up"):
        try:
            _collect(ctx)
        except (V.VMError, OSError, tarfile.TarError) as e:
            print(S.mask(f"pl e2e: copying artifacts and logs out failed: {e}", spec.secrets), file=sys.stderr)
    if ctx.get("launched"):
        if ctx["keep"]:
            print(f"VM kept: multipass shell {name}  (remove it with: multipass delete --purge {name})")
        else:
            try:
                V.delete(name)
            except V.VMError as e:
                print(S.mask(f"pl e2e: {e}", spec.secrets), file=sys.stderr)
    for d in ("logs", "artifacts"):
        _mask_tree(out / d, spec.secrets)
    for s in steps:
        if s["status"] in ("failed", "timeout"):
            s["tail"] = _tail(out, s)
        s.pop("note", None)
    results = R.parse_playwright(out / "artifacts" / ctx["report"]) if ctx["report"] else None
    run = {"repo": ctx["repo"].name, "ref": ctx["ref"], "sha": ctx["sha"],
           "vm": {"name": name, **dataclasses.asdict(spec.vm)}, "started": started, "finished": now_iso(),
           "steps": steps, "exit_code": code, "outcome": outcome, "artifacts_dir": "artifacts", "vm_root": APP}
    (out / "report.md").write_text(S.mask(R.render_markdown(run, results), spec.secrets))
    summary = _masked(R.summary_json(run, results), spec.secrets)
    (out / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    for n in ("env", "bundle.tar"):
        (ctx["stage"] / n).unlink(missing_ok=True)
    print(f"{R.VERDICT.get(outcome, outcome)}: {out / 'report.md'}")


def cmd_e2e(argv):
    ap = argparse.ArgumentParser(prog="pl e2e", description="Run the repo's Playwright suite in a throwaway "
                                 "Multipass VM, set up from .pl/e2e.yaml at REF.",
                                 epilog="Exit codes: 0 passed, 1 tests failed, 2 setup, service or VM failure or a bad "
                                 ".pl/e2e.yaml. --out gets report.md, summary.json, logs/ and artifacts/.")
    ap.add_argument("repo", nargs="?", help="the app repository (default: the git root of this folder)")
    ap.add_argument("--ref", default="HEAD", help="the commit, branch or tag to test (default HEAD)")
    ap.add_argument("--out", help="a plain folder for the results (default ~/pl-e2e/<repo>/<timestamp>)")
    ap.add_argument("--keep-vm", action="store_true", help="keep the VM afterwards, for debugging")
    a = ap.parse_args(argv)
    try:
        ctx = _prepare(a, os.environ)
    except (Stop, OSError) as e:
        print(e if isinstance(e, Stop) else f"pl e2e: could not write the stage folder: {e}", file=sys.stderr)
        return 2
    steps, started = _plan(ctx["spec"]), now_iso()
    outcome, code = "infra_failed", 2
    try:
        outcome, code = _drive(ctx, steps)
    except (KeyboardInterrupt, V.VMError, OSError) as e:
        note = "interrupted (Ctrl-C)" if isinstance(e, KeyboardInterrupt) else S.mask(str(e), ctx["spec"].secrets)
        print(f"pl e2e: {note}; collecting logs and cleaning up", file=sys.stderr)
        for s, _ in steps:
            if s["status"] == "running":
                s["status"], s["note"] = "failed", note
    finally:
        _finish(ctx, [s for s, _ in steps], outcome, code, started)
    return code
