import subprocess

import pytest

from pl import e2e_vm as V

OUT = "/home/u/pl-e2e/app/20261022-101500"
SCRIPT, LOG = "/home/ubuntu/.pl-e2e/step-1.sh", "/home/ubuntu/.pl-e2e/logs/setup-1-infra.log"


@pytest.fixture
def calls(monkeypatch):
    """Fake _run: records (argv, timeout); answers from the replies list (code, output), else (0, "")."""
    class Rec(list):
        pass
    rec = Rec()
    rec.replies = []

    def run(argv, timeout=None):
        assert isinstance(argv, list) and all(isinstance(a, str) for a in argv)
        rec.append((argv, timeout))
        return rec.replies.pop(0) if rec.replies else (0, "")
    monkeypatch.setattr(V, "_run", run)
    return rec


def test_vm_name_slugs_and_fits():
    assert V.vm_name("DiquisFootball", "20261022-101500") == "pl-e2e-diquisfootball-20261022-101500"
    assert V.vm_name("my_app.v2", "1") == "pl-e2e-my-app-v2-1"
    assert V.vm_name("---", "1") == "pl-e2e-app-1"
    n = V.vm_name("x" * 200 + "-y", "20261022-101500")
    assert len(n) <= 63 and n.endswith("-20261022-101500") and "--" not in n
    n = V.vm_name("a" * 40 + "-" + "b" * 40, "20261022-101500")
    assert len(n) <= 63 and "--" not in n
    import re
    assert re.fullmatch(r"[a-z][a-z0-9-]*[a-z0-9]", n)


def test_cloud_init_lists_quoted_packages():
    text = V.cloud_init(["docker.io", "docker-compose-v2", "curl"])
    assert text.splitlines() == ["#cloud-config", "package_update: true", "packages:",
                                 '  - "docker.io"', '  - "docker-compose-v2"', '  - "curl"']
    assert V.cloud_init([]).splitlines() == ["#cloud-config", "package_update: true", "packages: []"]


def test_cloud_init_is_valid_yaml():
    yaml = pytest.importorskip("yaml")
    assert yaml.safe_load(V.cloud_init(["a", 'we"ird'])) == {"package_update": True, "packages": ["a", 'we"ird']}


@pytest.mark.parametrize("path", ["/tmp/x/cloud-init.yaml", "/tmp", "/home/u/.pl-e2e/x", "/home/u/.cache/a/b",
                                  "~/.x/y"])
def test_check_host_path_rejects_tmp_and_hidden(path):
    with pytest.raises(V.VMError, match="--out ~/pl-e2e"):
        V.check_host_path(path)


def test_check_host_path_accepts_plain_folder():
    assert V.check_host_path(OUT + "/stage/app.tar.gz") == OUT + "/stage/app.tar.gz"
    assert V.check_host_path("/home/u/tmpstuff/a") == "/home/u/tmpstuff/a"


def test_launch_argv(calls):
    V.launch("pl-e2e-app-1", "24.04", 4, "8G", "30G", OUT + "/stage/cloud-init.yaml")
    argv, timeout = calls[0]
    assert argv == ["multipass", "launch", "24.04", "--name", "pl-e2e-app-1", "--cpus", "4", "--memory", "8G",
                    "--disk", "30G", "--cloud-init", OUT + "/stage/cloud-init.yaml", "--timeout", "900"]
    assert timeout > 900


def test_launch_failure_raises(calls):
    calls.replies.append((1, "launch failed: image not found"))
    with pytest.raises(V.VMError, match="image not found"):
        V.launch("pl-e2e-app-1", "99.04", 1, "1G", "5G", OUT + "/c.yaml", timeout=60)


def test_launch_refuses_tmp_cloud_init(calls):
    with pytest.raises(V.VMError):
        V.launch("pl-e2e-app-1", "24.04", 1, "1G", "5G", "/tmp/c.yaml")
    assert calls == []


def test_transfer_in_and_out(calls):
    V.transfer_in("vm1", OUT + "/stage/app.tar.gz", "/home/ubuntu/app.tar.gz")
    V.transfer_out("vm1", "/home/ubuntu/out.tar.gz", OUT + "/out.tar.gz")
    assert [c[0] for c in calls] == [
        ["multipass", "transfer", OUT + "/stage/app.tar.gz", "vm1:/home/ubuntu/app.tar.gz"],
        ["multipass", "transfer", "vm1:/home/ubuntu/out.tar.gz", OUT + "/out.tar.gz"]]
    calls.replies.append((2, "no such file"))
    with pytest.raises(V.VMError, match="no such file"):
        V.transfer_out("vm1", "/nope", OUT + "/x")
    with pytest.raises(V.VMError):
        V.transfer_in("vm1", "/home/u/.hidden/a", "/x")


def test_exec_step_passes_paths_as_positional_args(calls):
    calls.replies.append((3, ""))
    assert V.exec_step("vm1", SCRIPT, LOG, "/home/ubuntu/app", 1800) == 3
    argv, timeout = calls[0]
    assert argv == ["multipass", "exec", "vm1", "--working-directory", "/home/ubuntu/app", "--", "bash", "-c",
                    V.STEP_SH, "_", SCRIPT, LOG]
    assert V.STEP_SH == 'set -o pipefail; bash -euo pipefail "$1" > "$2" 2>&1'
    assert timeout == 1800
    assert sum(SCRIPT in a for a in argv) == 1 and sum(LOG in a for a in argv) == 1   # never inside the -c string


def test_exec_step_paths_with_shell_metacharacters_stay_separate(calls):
    evil = "/home/ubuntu/x; rm -rf / #.sh"
    V.exec_step("vm1", evil, "$(touch /pwned)", "/home/ubuntu/app", 10)
    argv = calls[0][0]
    assert argv[-2:] == [evil, "$(touch /pwned)"] and argv[-4] == V.STEP_SH


def test_exec_step_timeout_is_distinct(calls):
    calls.replies.append((V.TIMEOUT, "timed out"))
    assert V.exec_step("vm1", SCRIPT, LOG, "/home/ubuntu/app", 5) == V.TIMEOUT == 124


def test_start_service_detaches_with_constant_string(calls):
    assert V.start_service("vm1", SCRIPT, LOG, "/home/ubuntu/app") == 0
    argv = calls[0][0]
    assert argv == ["multipass", "exec", "vm1", "--working-directory", "/home/ubuntu/app", "--", "bash", "-c",
                    V.SERVICE_SH, "_", SCRIPT, LOG]
    assert "setsid" in V.SERVICE_SH and "nohup" in V.SERVICE_SH and "$1" in V.SERVICE_SH and "$2" in V.SERVICE_SH
    assert "/dev/null" in V.SERVICE_SH   # stdin detached so multipass exec returns


class Clock:
    def __init__(self):
        self.t, self.slept = 0.0, []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def test_wait_ready_http_success(calls):
    clock = Clock()
    calls.replies.extend([(7, "refused"), (22, "503"), (0, "")])
    assert V.wait_ready("vm1", "http://localhost:5000/health", 60, clock=clock, sleep=clock.sleep) is True
    assert [c[0] for c in calls] == [["multipass", "exec", "vm1", "--", "curl", "-fsS", "-o", "/dev/null",
                                      "--max-time", "5", "http://localhost:5000/health"]] * 3
    assert clock.slept == [2, 2]


def test_wait_ready_https_timeout(calls):
    clock = Clock()
    calls.replies.extend([(7, "")] * 100)
    assert V.wait_ready("vm1", "https://localhost/x", 10, interval=3, clock=clock, sleep=clock.sleep) is False
    assert clock.t <= 13 and len(calls) <= 5


def test_wait_ready_tcp_port_and_host(calls):
    clock = Clock()
    assert V.wait_ready("vm1", "tcp:5432", 30, clock=clock, sleep=clock.sleep) is True
    assert calls[0][0] == ["multipass", "exec", "vm1", "--", "bash", "-c", V.TCP_SH, "_", "localhost", "5432"]
    assert V.TCP_SH == 'exec 3<>/dev/tcp/"$1"/"$2"'
    V.wait_ready("vm1", "tcp:db.local:6379", 30, clock=clock, sleep=clock.sleep)
    assert calls[1][0][-2:] == ["db.local", "6379"]


def test_wait_ready_tcp_timeout(calls):
    clock = Clock()
    calls.replies.extend([(1, "")] * 100)
    assert V.wait_ready("vm1", "tcp:5432", 5, interval=2, clock=clock, sleep=clock.sleep) is False
    assert clock.t <= 7


@pytest.mark.parametrize("ready", ["ftp://x", "tcp:", "tcp:abc", "tcp:h:p", "localhost:80", "tcp:1:2:3"])
def test_wait_ready_rejects_bad_target(calls, ready):
    with pytest.raises(V.VMError):
        V.wait_ready("vm1", ready, 5, clock=Clock(), sleep=lambda s: None)
    assert calls == []


def test_exists_and_delete(calls):
    calls.replies.extend([(0, "Name: vm1"), (1, "instance \"vm2\" does not exist")])
    assert V.exists("vm1") is True and V.exists("vm2") is False
    assert [c[0] for c in calls] == [["multipass", "info", "vm1"], ["multipass", "info", "vm2"]]
    calls.clear()
    V.delete("vm1")
    assert calls[0][0] == ["multipass", "delete", "--purge", "vm1"]


def test_delete_tolerates_missing_vm(calls):
    calls.replies.extend([(2, "instance \"vm1\" does not exist"), (2, "does not exist")])
    V.delete("vm1")   # no raise
    assert [c[0][1] for c in calls] == ["delete", "info"]


def test_delete_raises_when_vm_still_there(calls):
    calls.replies.extend([(1, "busy"), (0, "Name: vm1")])
    with pytest.raises(V.VMError, match="busy"):
        V.delete("vm1")


def test_run_timeout_and_missing_binary(monkeypatch):
    def slow(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw.get("timeout"))
    monkeypatch.setattr(V.subprocess, "run", slow)
    code, out = V._run(["multipass", "list"], timeout=1)
    assert code == V.TIMEOUT and "timed out" in out

    def missing(argv, **kw):
        raise FileNotFoundError("multipass")
    monkeypatch.setattr(V.subprocess, "run", missing)
    code, out = V._run(["multipass", "list"])
    assert code == 127 and "multipass" in out


def test_run_returns_code_and_output(monkeypatch):
    seen = {}

    def ok(argv, **kw):
        seen.update(kw, argv=argv)
        return subprocess.CompletedProcess(argv, 0, stdout="hi\n")
    monkeypatch.setattr(V.subprocess, "run", ok)
    assert V._run(["multipass", "version"], timeout=5) == (0, "hi\n")
    assert seen["timeout"] == 5 and not seen.get("shell") and seen["stderr"] == subprocess.STDOUT
