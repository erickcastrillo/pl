"""pl e2e orchestration, with a fake Multipass layer and a fake git seam: no VM, no real git, no real HOME."""
import io
import json
import os
import stat
import sys
import tarfile
from pathlib import Path

import pytest

from pl import cli, e2e
from pl import config as C

SHA = "a" * 40
SECRET = "s3cr3t-value-xyz"
YAML = """version: 1
packages: [curl]
env:
  MODE: test
  API_KEY: ${E2E_TEST_KEY}
setup:
  - name: infra
    run: echo infra
  - name: build
    run: echo build
services:
  - name: api
    run: ./api
    ready: http://localhost:5000/health
tests:
  dir: web
  run: npx playwright test --trace=on
  report: results.json
artifacts: [web/test-results]
"""
FAILING_REPORT = {"suites": [{"title": "a.spec.ts", "specs": [{"title": "logs in", "file": "a.spec.ts", "line": 3,
                  "tests": [{"status": "unexpected", "projectName": "chromium", "results": [{"status": "failed",
                             "error": {"message": f"boom {SECRET}"},
                             "attachments": [{"name": "trace", "contentType": "application/zip",
                                              "path": "/home/ubuntu/app/web/test-results/t/trace.zip"}]}]}]}]}],
                  "stats": {"expected": 1, "unexpected": 1, "flaky": 0, "skipped": 0, "duration": 1000}}


class FakeVM:
    """Stands in for pl.e2e_vm: records calls; logs and artifacts live in a dict 'inside the VM'."""

    def __init__(self, repo):
        self.calls, self.codes, self.ready, self.files = [], {}, True, {}
        self.raise_on, self.repo = None, repo
        self.report = {"suites": [], "stats": {"expected": 2, "unexpected": 0, "flaky": 0, "skipped": 0,
                                               "duration": 1000}}

    def _call(self, *c):
        self.calls.append(c)
        if self.raise_on and c[0] == self.raise_on[0] and (len(self.raise_on) == 2 or self.raise_on[1] in c[-1]):
            raise self.raise_on[-1]() if isinstance(self.raise_on[-1], type) else self.raise_on[-1]

    def launch(self, name, image, cpus, memory, disk, cloud_init_path, timeout=900):
        assert Path(cloud_init_path).read_text().startswith("#cloud-config")
        self._call("launch", name)

    def transfer_in(self, name, host, vm):
        self._call("transfer_in", vm)

    def exec_step(self, name, script, log, cwd, timeout):
        self._call("exec", os.path.basename(script))
        assert timeout > 0
        base = os.path.basename(script)
        if base.startswith(("setup", "tests")):
            self.files[f"logs/{os.path.basename(log)}"] = f"running {base} with {SECRET}\nline 2\n"
        if base == "tests.sh":
            self.files["web/results.json"] = json.dumps(self.report)
            self.files["web/test-results/t/trace.zip"] = "zip"
            self.cwd_tests = cwd
        return self.codes.get(base, 0)

    def start_service(self, name, script, log, cwd, timeout=60):
        self._call("start_service", os.path.basename(script))
        self.files[f"logs/{os.path.basename(log)}"] = "service up\n"
        return 0

    def wait_ready(self, name, ready, timeout, **_):
        self._call("wait_ready", ready)
        return self.ready

    def transfer_out(self, name, vm, host):
        self._call("transfer_out", vm)
        logs = "logs" in os.path.basename(vm)
        with tarfile.open(host, "w:gz") as t:
            for path, text in self.files.items():
                if path.startswith("logs/") == logs:
                    data = text.encode()
                    info = tarfile.TarInfo(path)
                    info.size = len(data)
                    t.addfile(info, io.BytesIO(data))

    def delete(self, name):
        self._call("delete", name)

    def names(self):
        return [c[0] if c[0] in ("launch", "delete") else f"{c[0]}:{c[1]}" for c in self.calls]


@pytest.fixture
def env(monkeypatch, tmp_path):
    repo = tmp_path / "myapp"
    repo.mkdir()
    vm = FakeVM(repo)
    for fn in ("launch", "transfer_in", "transfer_out", "exec_step", "start_service", "wait_ready", "delete"):
        monkeypatch.setattr(e2e.V, fn, getattr(vm, fn))
    monkeypatch.setattr(e2e.V, "check_host_path", lambda p: os.path.abspath(p))   # tmp_path lives under /tmp
    monkeypatch.setenv("E2E_TEST_KEY", SECRET)
    git = {"yaml": YAML, "calls": []}

    def run(argv, timeout=120):
        assert isinstance(argv, list) and argv[0] == "git"
        git["calls"].append(argv)
        args = argv[3:]
        if args[:2] == ["rev-parse", "--show-toplevel"]:
            return 0, str(repo) + "\n", ""
        if args[:2] == ["rev-parse", "--verify"]:
            return 0, SHA + "\n", ""
        if args[0] == "show":
            if git["yaml"] is None:
                return 128, "", "fatal: path '.pl/e2e.yaml' does not exist in 'HEAD'"
            return 0, git["yaml"], ""
        if args[0] == "archive":
            Path(args[args.index("-o") + 1]).write_bytes(b"tar")
            return 0, "", ""
        raise AssertionError(argv)
    monkeypatch.setattr(e2e, "_run", run)
    vm.git = git
    vm.out = tmp_path / "out"
    return vm


def go(vm, *extra):
    return e2e.cmd_e2e([str(vm.repo), "--out", str(vm.out), *extra])


def summary(vm):
    return json.loads((vm.out / "summary.json").read_text())


def test_happy_path_runs_every_step_in_order_and_exits_0(env, capsys):
    assert go(env) == 0
    assert env.names() == ["launch", "transfer_in:/home/ubuntu/pl-e2e-bundle.tar",
                           "transfer_in:/home/ubuntu/pl-e2e-boot.sh", "exec:pl-e2e-boot.sh",
                           "exec:setup-1.sh", "exec:setup-2.sh", "start_service:service-api.sh",
                           "wait_ready:http://localhost:5000/health", "exec:tests.sh", "exec:collect.sh",
                           "transfer_out:/home/ubuntu/pl-e2e-artifacts.tar.gz",
                           "transfer_out:/home/ubuntu/pl-e2e-logs.tar.gz", "delete"]
    assert env.calls[0][1].startswith("pl-e2e-myapp-")
    assert env.cwd_tests == "/home/ubuntu/app/web"
    s = summary(env)
    assert s["outcome"] == "passed" and s["sha"] == SHA and s["tests"]["passed"] == 2
    assert [(x["name"], x["status"]) for x in s["steps"]] == [
        ("start-vm", "ok"), ("infra", "ok"), ("build", "ok"), ("api", "ok"), ("tests", "ok")]
    assert (env.out / "logs" / "setup-1-infra.log").exists()
    assert (env.out / "artifacts" / "web" / "test-results" / "t" / "trace.zip").exists()
    out = capsys.readouterr().out
    assert "setup infra: ok" in out and str(env.out / "report.md") in out
    stage = env.out / "stage"
    tests_sh = (stage / "tests.sh").read_text()
    assert "PLAYWRIGHT_JSON_OUTPUT_NAME=/home/ubuntu/app/web/results.json" in tests_sh
    assert "source /home/ubuntu/.pl-e2e/env" in (stage / "setup-1.sh").read_text()
    assert "curl" in (stage / "cloud-init.yaml").read_text()


def test_failing_setup_step_skips_the_rest_but_collects_and_deletes(env):
    env.codes["setup-1.sh"] = 3
    assert go(env) == 2
    names = env.names()
    assert "exec:setup-2.sh" not in names and "exec:tests.sh" not in names
    assert not any(n.startswith(("start_service", "wait_ready")) for n in names)
    assert names[-4:] == ["exec:collect.sh", "transfer_out:/home/ubuntu/pl-e2e-artifacts.tar.gz",
                          "transfer_out:/home/ubuntu/pl-e2e-logs.tar.gz", "delete"]
    s = summary(env)
    assert s["outcome"] == "setup_failed"
    assert [x["status"] for x in s["steps"]] == ["ok", "failed", "skipped", "skipped", "skipped"]
    report = (env.out / "report.md").read_text()
    assert "Failed step: infra" in report and "line 2" in report


def test_service_never_ready_exits_2(env):
    env.ready = False
    assert go(env) == 2
    assert "exec:tests.sh" not in env.names() and env.names()[-1] == "delete"
    assert summary(env)["outcome"] == "setup_failed"


def test_failing_tests_exit_1_with_the_failure_in_the_report(env):
    env.codes["tests.sh"] = 1
    env.report = FAILING_REPORT
    assert go(env) == 1
    assert summary(env)["outcome"] == "tests_failed"
    report = (env.out / "report.md").read_text()
    assert "logs in" in report and "artifacts/web/test-results/t/trace.zip" in report
    assert SECRET not in report and "boom ***" in report


def test_ctrl_c_mid_run_still_collects_and_deletes(env):
    env.raise_on = ("exec", "setup-2.sh", KeyboardInterrupt)
    assert go(env) == 2
    names = env.names()
    assert "exec:collect.sh" in names and names[-1] == "delete" and "exec:tests.sh" not in names
    assert summary(env)["outcome"] == "infra_failed"


def test_launch_failure_is_infra_and_still_deletes(env):
    env.raise_on = ("launch", e2e.V.VMError("no room"))
    assert go(env) == 2
    assert env.names() == ["launch", "delete"]
    s = summary(env)
    assert s["outcome"] == "infra_failed" and s["steps"][0]["status"] == "failed"
    assert "no room" in (env.out / "report.md").read_text()


def test_keep_vm_skips_delete_and_prints_shell(env, capsys):
    assert go(env, "--keep-vm") == 0
    assert "delete" not in env.names()
    assert f"multipass shell {env.calls[0][1]}" in capsys.readouterr().out


def test_secrets_masked_and_env_file_private_then_removed(env, monkeypatch):
    modes = {}
    real = e2e._bundle

    def spy(stage, names):
        modes["env"] = stat.S_IMODE((stage / "env").stat().st_mode)
        assert f"export API_KEY={SECRET}" in (stage / "env").read_text()
        return real(stage, names)
    monkeypatch.setattr(e2e, "_bundle", spy)
    env.codes["setup-2.sh"] = 1
    assert go(env) == 2
    assert modes["env"] == 0o600
    assert not (env.out / "stage" / "env").exists() and not (env.out / "stage" / "bundle.tar").exists()
    for p in env.out.rglob("*"):
        if p.is_file():
            assert SECRET.encode() not in p.read_bytes(), p
    assert "running setup-2.sh with ***" in (env.out / "logs" / "setup-2-build.log").read_text()
    assert "with ***" in (env.out / "report.md").read_text()


def test_missing_yaml_is_a_clear_message_and_no_vm(env, capsys):
    env.git["yaml"] = None
    assert go(env) == 2
    assert env.calls == []
    assert ".pl/e2e.yaml" in capsys.readouterr().err


def test_invalid_yaml_prints_problems_and_no_vm(env, capsys):
    env.git["yaml"] = "version: 2\ntests: {run: x}\nbogus: 1\n"
    assert go(env) == 2
    err = capsys.readouterr().err
    assert "version: must be 1" in err and "bogus: unknown key" in err
    assert env.calls == []


def test_unset_secret_var_is_a_problem(env, capsys, monkeypatch):
    monkeypatch.delenv("E2E_TEST_KEY")
    assert go(env) == 2 and env.calls == []
    assert "${E2E_TEST_KEY} is not set" in capsys.readouterr().err


def test_default_out_is_a_plain_folder_in_home(env, monkeypatch):
    monkeypatch.chdir(env.repo)
    assert e2e.cmd_e2e([]) == 0
    home = Path(os.environ["HOME"])
    outs = list((home / "pl-e2e" / "myapp").iterdir())
    assert len(outs) == 1 and (outs[0] / "report.md").exists()
    rel = outs[0].relative_to(home)
    assert not any(part.startswith(".") for part in rel.parts)
    assert len(outs[0].name) == 15 and outs[0].name[8] == "-"
    assert ["git", "-C", str(env.repo), "rev-parse", "--verify", "HEAD^{commit}"] in env.git["calls"]


def test_ref_option_is_used(env):
    assert go(env, "--ref", "feature/x") == 0
    assert any(a[-1] == "feature/x^{commit}" for a in env.git["calls"])


def test_cli_runs_e2e_without_a_profile(monkeypatch):
    C.CONFIG_DIR = None
    got = []
    monkeypatch.setattr(e2e, "cmd_e2e", lambda argv: got.append(argv) or 0)
    monkeypatch.setattr(sys, "argv", ["pl", "e2e", "/some/repo", "--keep-vm"])
    monkeypatch.setattr(cli.config, "load", lambda *_: pytest.fail("pl e2e must not load a profile"))
    assert cli.main() == 0
    assert got == [["/some/repo", "--keep-vm"]]


def test_whole_run_timeout_stops_before_the_next_step(env, monkeypatch):
    ticks = iter([0, 0, 0, 10, 10, 10])        # deadline at 0 + 3600; then far past it
    monkeypatch.setattr(e2e, "_clock", lambda: next(ticks, 99999))
    assert go(env) == 2
    s = summary(env)
    assert [x["status"] for x in s["steps"]][:3] == ["ok", "timeout", "skipped"]
    assert "timeout (3600s) ran out" in (env.out / "report.md").read_text()
    assert env.names()[-1] == "delete"
