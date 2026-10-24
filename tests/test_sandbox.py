"""Tests for the Multipass sandbox VM lifecycle and config."""
import argparse
import json
import shutil
import subprocess
import sys

import pytest

from pl import cli
from pl import config as C
from pl import sandbox


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("PL_CONFIG_DIR", "PL_TMUX_SESSION"):
        monkeypatch.delenv(var, raising=False)
    work = tmp_path / "work"
    work.mkdir()
    d = tmp_path / ".pl-t"
    d.mkdir()
    (d / "config.toml").write_text(f'[paths]\nwork_dir = "{work}"\n')
    C.load("t")
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


class FakeMultipass:
    def __init__(self):
        self.vms = {}
        self.calls = []
        self.inputs = []

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        if "input" in kw:
            self.inputs.append((list(argv), kw["input"]))
        cmd = argv[1] if len(argv) > 1 else ""
        if cmd == "launch":
            name = argv[argv.index("--name") + 1]
            mounts = {}
            for i, a in enumerate(argv):
                if a == "--mount":
                    src, _, tgt = argv[i + 1].partition(":")
                    mounts[tgt] = src
            self.vms[name] = {
                "state": "Running",
                "mounts": mounts,
                "argv": list(argv),
            }
            return subprocess.CompletedProcess(argv, 0, "", "")
        elif cmd == "info":
            name = [a for a in argv[2:] if not a.startswith("-")][0]
            if name not in self.vms:
                return subprocess.CompletedProcess(argv, 2, "", f"info failed: instance '{name}' does not exist\n")
            vm = self.vms[name]
            mounts_json = {tgt: {"source_path": src} for tgt, src in vm["mounts"].items()}
            data = {"info": {name: {"state": vm["state"], "mounts": mounts_json}}}
            return subprocess.CompletedProcess(argv, 0, json.dumps(data), "")
        elif cmd == "start":
            name = argv[2]
            if name not in self.vms:
                return subprocess.CompletedProcess(argv, 2, "", f"start failed: instance '{name}' does not exist\n")
            self.vms[name]["state"] = "Running"
            return subprocess.CompletedProcess(argv, 0, "", "")
        elif cmd == "stop":
            name = argv[2]
            if name not in self.vms:
                return subprocess.CompletedProcess(argv, 2, "", f"stop failed: instance '{name}' does not exist\n")
            self.vms[name]["state"] = "Stopped"
            return subprocess.CompletedProcess(argv, 0, "", "")
        elif cmd == "delete":
            name = argv[-1]
            if name in self.vms:
                del self.vms[name]
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, "", "")


@pytest.fixture
def fake_mp(monkeypatch):
    mp = FakeMultipass()
    monkeypatch.setattr(sandbox, "_run", mp)
    monkeypatch.setattr(shutil, "which", lambda cmd: "/usr/bin/multipass" if cmd == "multipass" else None)
    return mp


def test_sandbox_settings_default_off():
    assert C.SANDBOX == {}
    assert sandbox.settings() == sandbox.DEFAULTS


def test_config_reads_the_sandbox_table(fake_home):
    (fake_home / ".pl-t" / "config.toml").write_text(
        f'[paths]\nwork_dir = "{fake_home / "work"}"\n'
        '[sandbox]\nenabled = true\ncpus = 4\nmemory = "8G"\n'
    )
    C.load("t")
    assert sandbox.settings() == {"enabled": True, "cpus": 4, "memory": "8G", "disk": "20G", "allow": []}


def test_validate_checks_the_sandbox_table():
    good = {"sandbox": {"enabled": True, "cpus": 4, "memory": "8G", "disk": "30G"}}
    assert C.validate(good) == []

    bad_cases = [
        ({"enabled": "yes"}, "sandbox.enabled must be true or false"),
        ({"cpus": 0}, "sandbox.cpus must be a whole number of at least 1"),
        ({"cpus": True}, "sandbox.cpus must be a whole number of at least 1"),
        ({"memory": 4}, 'sandbox.memory must be a size like "4G" or "512M"'),
        ({"disk": "lots"}, 'sandbox.disk must be a size like "4G" or "512M"'),
        ({"image": "x"}, "sandbox.image is not a setting; the keys are enabled, cpus, memory, disk, allow"),
    ]
    for sb, expected_err in bad_cases:
        errs = C.validate({"sandbox": sb})
        assert any(expected_err in e for e in errs), f"Expected {expected_err!r} in {errs}"


@pytest.mark.parametrize("action", ["create", "status", "start", "stop", "destroy"])
def test_no_multipass_any_command_fails_and_creates_nothing(action, fake_mp, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda cmd: None)
    with pytest.raises(SystemExit) as exc:
        sandbox.cmd_sandbox(argparse.Namespace(action=action, yes=False))
    msg = str(exc.value)
    assert "Multipass" in msg and "install" in msg
    assert fake_mp.calls == []


def test_create_launches_named_vm_with_only_the_two_mounts(fake_mp, capsys):
    sandbox.cmd_sandbox(argparse.Namespace(action="create", yes=False))
    assert "pl-sbx-t" in fake_mp.vms
    launch_argv = fake_mp.vms["pl-sbx-t"]["argv"]
    mount_indices = [i for i, x in enumerate(launch_argv) if x == "--mount"]
    assert len(mount_indices) == 2
    mount_vals = [launch_argv[i + 1] for i in mount_indices]
    work_str = str(C.WORK_DIR.resolve())
    wt_str = str(C.WORKTREES_DIR.resolve())
    assert f"{work_str}:{work_str}" in mount_vals
    assert f"{wt_str}:{wt_str}" in mount_vals
    for forbidden in (str(C.HOME.resolve()), ".ssh", ".pl-", ".claude"):
        for val in mount_vals:
            assert val != f"{C.HOME.resolve()}:{C.HOME.resolve()}"
            assert forbidden not in val or "work" in val
    out = capsys.readouterr().out
    assert "Running" in out
    assert work_str in out
    assert wt_str in out


def test_create_uses_the_sandbox_table(fake_home, fake_mp):
    (fake_home / ".pl-t" / "config.toml").write_text(
        f'[paths]\nwork_dir = "{fake_home / "work"}"\n'
        '[sandbox]\ncpus = 4\nmemory = "8G"\ndisk = "30G"\n'
    )
    C.load("t")
    sandbox.cmd_sandbox(argparse.Namespace(action="create", yes=False))
    argv = fake_mp.vms["pl-sbx-t"]["argv"]
    assert argv[argv.index("--cpus") + 1] == "4"
    assert argv[argv.index("--memory") + 1] == "8G"
    assert argv[argv.index("--disk") + 1] == "30G"


def test_create_twice_says_exists_and_launches_once(fake_mp, capsys):
    sandbox.cmd_sandbox(argparse.Namespace(action="create", yes=False))
    assert len([c for c in fake_mp.calls if c[1] == "launch"]) == 1
    capsys.readouterr()
    sandbox.cmd_sandbox(argparse.Namespace(action="create", yes=False))
    assert len([c for c in fake_mp.calls if c[1] == "launch"]) == 1
    out = capsys.readouterr().out
    assert "already exists" in out


def test_create_refuses_when_work_dir_is_home(fake_home, fake_mp):
    (fake_home / ".pl-t" / "config.toml").write_text("")
    C.load("t")
    with pytest.raises(SystemExit) as exc:
        sandbox.cmd_sandbox(argparse.Namespace(action="create", yes=False))
    assert str(fake_home.resolve()) in str(exc.value)
    assert fake_mp.calls == []


def test_create_refuses_a_work_dir_holding_a_credential_folder(fake_home, fake_mp):
    work = fake_home / "work"
    creds = work / ".claude-acme"
    creds.mkdir()
    (fake_home / ".pl-t" / "config.toml").write_text(
        f'[paths]\nwork_dir = "{work}"\n'
        f'[accounts.acme]\nconfig_dir = "{creds}"\n'
    )
    C.load("t")
    with pytest.raises(SystemExit) as exc:
        sandbox.cmd_sandbox(argparse.Namespace(action="create", yes=False))
    assert "work" in str(exc.value)
    assert fake_mp.calls == []


def test_two_profiles_get_separate_vms_and_mounts(fake_home, fake_mp):
    work_a = fake_home / "work_a"
    work_b = fake_home / "work_b"
    work_a.mkdir()
    work_b.mkdir()
    (fake_home / ".pl-a").mkdir()
    (fake_home / ".pl-a" / "config.toml").write_text(f'[paths]\nwork_dir = "{work_a}"\n')
    (fake_home / ".pl-b").mkdir()
    (fake_home / ".pl-b" / "config.toml").write_text(f'[paths]\nwork_dir = "{work_b}"\n')

    C.load("a")
    sandbox.cmd_sandbox(argparse.Namespace(action="create", yes=False))
    C.load("b")
    sandbox.cmd_sandbox(argparse.Namespace(action="create", yes=False))

    assert "pl-sbx-a" in fake_mp.vms
    assert "pl-sbx-b" in fake_mp.vms
    mounts_a = fake_mp.vms["pl-sbx-a"]["mounts"]
    mounts_b = fake_mp.vms["pl-sbx-b"]["mounts"]
    assert str(work_a.resolve()) in mounts_a.values()
    assert str(work_b.resolve()) not in mounts_a.values()
    assert str(work_b.resolve()) in mounts_b.values()
    assert str(work_a.resolve()) not in mounts_b.values()


def test_stop_then_start_keeps_the_mounts(fake_mp, capsys):
    sandbox.cmd_sandbox(argparse.Namespace(action="create", yes=False))
    sandbox.cmd_sandbox(argparse.Namespace(action="stop", yes=False))
    assert fake_mp.vms["pl-sbx-t"]["state"] == "Stopped"
    capsys.readouterr()
    sandbox.cmd_sandbox(argparse.Namespace(action="status", yes=False))
    out = capsys.readouterr().out
    assert "Stopped" in out
    assert str(C.WORK_DIR.resolve()) in out

    sandbox.cmd_sandbox(argparse.Namespace(action="start", yes=False))
    assert fake_mp.vms["pl-sbx-t"]["state"] == "Running"
    capsys.readouterr()
    sandbox.cmd_sandbox(argparse.Namespace(action="status", yes=False))
    out = capsys.readouterr().out
    assert "Running" in out
    assert str(C.WORK_DIR.resolve()) in out


def test_destroy_needs_yes_then_purges_and_keeps_host_files(fake_mp, capsys):
    sandbox.cmd_sandbox(argparse.Namespace(action="create", yes=False))
    test_file = C.WORK_DIR / "code.py"
    test_file.write_text("print('hello')")

    with pytest.raises(SystemExit) as exc:
        sandbox.cmd_sandbox(argparse.Namespace(action="destroy", yes=False))
    assert "--yes" in str(exc.value)
    assert not any(c[1] == "delete" for c in fake_mp.calls)

    sandbox.cmd_sandbox(argparse.Namespace(action="destroy", yes=True))
    assert any(c[1:3] == ["delete", "--purge"] for c in fake_mp.calls)
    assert "pl-sbx-t" not in fake_mp.vms

    with pytest.raises(SystemExit) as exc:
        sandbox.cmd_sandbox(argparse.Namespace(action="status", yes=False))
    assert "create" in str(exc.value)

    assert test_file.read_text() == "print('hello')"


@pytest.mark.parametrize("action", ["status", "start", "stop"])
def test_without_a_vm_status_start_stop_say_create_first(fake_mp, action):
    with pytest.raises(SystemExit) as exc:
        sandbox.cmd_sandbox(argparse.Namespace(action=action, yes=False))
    assert "run pl sandbox create first" in str(exc.value)


def test_stage_launch_is_unchanged_by_the_sandbox_table(fake_home):
    from pl import harnesses
    h = harnesses.get("claude")
    C.ACCOUNTS["acme"] = {"harness": "claude", "config_dir": str(fake_home / ".claude-acme")}
    C.PROFILES["acme"] = fake_home / ".claude-acme"
    C.SANDBOX = {}
    s1 = harnesses.launch_script(h, "acme", "go", "sid", "run:1")
    C.SANDBOX = {"enabled": True}
    s2 = harnesses.launch_script(h, "acme", "go", "sid", "run:1")
    assert s1 == s2


def test_cli_sandbox_status_via_main(monkeypatch):
    called = []
    monkeypatch.setattr(sandbox, "cmd_sandbox", lambda a: called.append(a))
    monkeypatch.setattr(sys, "argv", ["pl", "--profile", "t", "sandbox", "status"])
    cli.main()
    assert len(called) == 1
    assert called[0].action == "status"


def fake_resolver(host, port):
    mapping = {
        "api.anthropic.com": [(2, None, None, None, ("1.1.1.10", 0))],
        "api.openai.com": [(2, None, None, None, ("2.2.2.10", 0))],
        "generativelanguage.googleapis.com": [(2, None, None, None, ("3.3.3.10", 0))],
        "github.com": [(2, None, None, None, ("140.82.112.4", 0))],
        "api.github.com": [(2, None, None, None, ("140.82.112.5", 0))],
        "allowed.host.com": [(2, None, None, None, ("4.4.4.10", 0)),
                             (10, None, None, None, ("2001:db8::4", 0, 0, 0))],
    }
    if host in mapping:
        return mapping[host]
    raise OSError(f"unknown host: {host}")


def test_the_ruleset_drops_by_default_and_allows_only_listed_hosts():
    assert sandbox.SANDBOX_HOSTS["claude"] == ["api.anthropic.com"]
    assert sandbox.SANDBOX_HOSTS["codex"] == ["api.openai.com"]
    assert sandbox.SANDBOX_HOSTS["antigravity"] == ["generativelanguage.googleapis.com"]

    # Base ruleset: drops by default, allows DNS and harness hosts, no code host by default
    text = sandbox.ruleset(harness="claude", resolver=fake_resolver)
    assert "policy drop;" in text
    assert "udp dport 53 accept" in text
    assert "tcp dport 53 accept" in text
    assert "1.1.1.10" in text
    assert "140.82.112.4" not in text
    assert "evil.com" not in text

    # With code_host, custom allow list (host and CIDR), and endpoint port
    text2 = sandbox.ruleset(
        harness="claude",
        endpoint_port=9090,
        allow=["allowed.host.com", "192.168.1.0/24"],
        include_code_host=True,
        resolver=fake_resolver,
    )
    assert "policy drop;" in text2
    assert "tcp dport 9090 accept" in text2
    assert "1.1.1.10" in text2
    assert "140.82.112.4" in text2
    assert "140.82.112.5" in text2
    assert "4.4.4.10" in text2
    assert "192.168.1.0/24" in text2
    assert "2001:db8::4" in text2
    assert "evil.com" not in text2


def test_a_bad_allow_entry_is_refused():
    good = {"sandbox": {"allow": ["github.com", "api.github.com", "10.0.0.0/8", "192.168.1.1"]}}
    assert C.validate(good) == []

    bad_cases = [
        ({"allow": "not-a-list"}, "sandbox.allow must be a list of host names or CIDRs"),
        ({"allow": ["http://github.com"]}, "must be a host name or CIDR"),
        ({"allow": ["github.com:443"]}, "must be a host name or CIDR"),
        ({"allow": ["github.com/path"]}, "must be a host name or CIDR"),
        ({"allow": ["bad host with spaces"]}, "must be a host name or CIDR"),
        ({"allow": ["999.999.999.999"]}, "must be a host name or CIDR"),
        ({"allow": ["; sudo rm -rf /"]}, "must be a host name or CIDR"),
        ({"allow": [123]}, "must be a host name or CIDR"),
    ]
    for sb, expected_err in bad_cases:
        errs = C.validate({"sandbox": sb})
        assert any(expected_err in e for e in errs), f"Expected {expected_err!r} in {errs}"


def test_the_ruleset_goes_on_stdin_not_argv(fake_mp):
    fake_mp.vms["pl-sbx-test"] = {"state": "Running", "mounts": {}, "argv": []}
    sandbox.apply_egress_rules("pl-sbx-test", harness="claude", endpoint_port=8080, resolver=fake_resolver)

    exec_calls = [c for c in fake_mp.calls if c[1] == "exec"]
    assert len(exec_calls) == 1
    argv = exec_calls[0]
    assert argv == [sandbox.MULTIPASS, "exec", "pl-sbx-test", "--", "sudo", "nft", "-f", "-"]
    assert not any("policy drop" in str(arg) for arg in argv)
    assert not any("table inet" in str(arg) for arg in argv)

    assert len(fake_mp.inputs) == 1
    input_argv, input_text = fake_mp.inputs[0]
    assert input_argv == argv
    assert "policy drop;" in input_text
    assert "tcp dport 8080 accept" in input_text

