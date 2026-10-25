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
        self.kws = []
        self.login_code = 0
        self.files = {}
        self.folders = {"/home/ubuntu", "/tmp"}
        self.file_log = []
        self.write_fails = False

    def _exec(self, argv, kw):
        cmd = argv[argv.index("--") + 1:]
        ok = subprocess.CompletedProcess(argv, 0, "", "")
        if cmd[0] == "which":
            return ok
        if cmd[0] == "sh":
            script, dest = cmd[2], cmd[4]
            if script.startswith("umask 077"):
                if dest.rsplit("/", 1)[0] not in self.folders:
                    return subprocess.CompletedProcess(argv, 1, "", f"sh: cannot create {dest}: No such directory\n")
                self.files[dest] = [0o600, ""]
            elif script.startswith("cat >"):
                if self.write_fails:
                    self.files[dest][1] = kw["input"][:3]
                    return subprocess.CompletedProcess(argv, 1, "", "write failed\n")
                self.files[dest][1] = kw["input"]
            self.file_log.append((dest, *self.files[dest]))
            return ok
        if cmd[0] == "rm":
            self.files.pop(cmd[-1], None)
            return ok
        return subprocess.CompletedProcess(argv, self.login_code, "", "")

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        self.kws.append(kw)
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
        elif cmd == "exec" and argv[2] in self.vms:
            return self._exec(argv, kw)
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


def test_a_sandboxed_stage_runs_inside_the_vm_in_its_worktree(fake_home):
    from pl import harnesses
    h = harnesses.get("claude")
    C.ACCOUNTS["acme"] = {"harness": "claude", "config_dir": str(fake_home / ".claude-acme")}
    C.PROFILES["acme"] = fake_home / ".claude-acme"
    C.SANDBOX = {"enabled": True}
    wt = fake_home / "worktrees" / "wt-1"
    wt.mkdir(parents=True)
    script = harnesses.launch_script(h, "acme", "do work", "sid-123", "run:1", stage="run", cwd=wt)
    assert "multipass exec pl-sbx-t --working-directory " + str(wt) in script
    assert " -- env " in script
    assert "claude" in script
    assert "do work" in script


def test_reading_ssh_or_another_profile_fails_inside_the_vm(fake_home):
    from pl import harnesses
    ssh_dir = fake_home / ".ssh"
    ssh_dir.mkdir()
    other_prof = fake_home / ".pl-other"
    other_prof.mkdir()
    ms = sandbox.mounts()
    for m in ms:
        assert not m.is_relative_to(ssh_dir) and not ssh_dir.is_relative_to(m)
        assert not m.is_relative_to(other_prof) and not other_prof.is_relative_to(m)

    h = harnesses.get("claude")
    C.ACCOUNTS["acme"] = {"harness": "claude", "config_dir": str(fake_home / ".claude-acme")}
    C.PROFILES["acme"] = fake_home / ".claude-acme"
    C.PROFILES["other"] = other_prof
    C.SANDBOX = {"enabled": True}
    wt = fake_home / "worktrees" / "wt-1"
    wt.mkdir(parents=True)
    script = harnesses.launch_script(h, "acme", "do work", "sid-123", "run:1", stage="run", cwd=wt)
    assert str(ssh_dir) not in script
    assert str(other_prof) not in script
    assert str(fake_home / ".claude-acme") not in script


def test_its_team_connectors_still_answer_through_the_host_endpoint(fake_home):
    from pl import connectors_proxy
    srv = connectors_proxy.ProxyServer(host="127.0.0.1", port=0)
    srv.start()
    try:
        scope = srv.register_session(
            session_id="sid-1",
            card_id="card-158",
            stage="run",
            team="core",
            connectors={"test_conn": lambda tool, args: {"echo": args.get("val")}},
        )
        resp = connectors_proxy.call_proxy(
            url=f"http://127.0.0.1:{srv.port}/connectors/test_conn",
            bearer=scope.bearer,
            body={"tool": "echo", "arguments": {"val": "ping"}},
        )
        assert resp == {"echo": "ping"}
    finally:
        srv.stop()


def test_the_session_bearer_works_only_for_its_card(fake_home):
    from pl import connectors_proxy
    with pytest.raises(ValueError) as exc:
        connectors_proxy.ProxyServer(host="0.0.0.0", port=0)
    assert "bridge address" in str(exc.value)

    srv = connectors_proxy.ProxyServer(host="127.0.0.1", port=0)
    srv.start()
    try:
        scope = srv.register_session(
            session_id="sid-1",
            card_id="card-158",
            stage="run",
            team="core",
            connectors={"test_conn": lambda tool, args: {"ok": True}},
        )
        url = f"http://127.0.0.1:{srv.port}/connectors/test_conn"

        with pytest.raises(connectors_proxy.ProxyError) as exc:
            connectors_proxy.call_proxy(url=url, bearer="wrong-token", body={"card_id": "card-158"})
        assert exc.value.status == 401

        resp = connectors_proxy.call_proxy(url=url, bearer=scope.bearer, body={"card_id": "card-158"})
        assert resp == {"ok": True}

        with pytest.raises(connectors_proxy.ProxyError) as exc:
            connectors_proxy.call_proxy(url=url, bearer=scope.bearer, body={"card_id": "other-999"})
        assert exc.value.status == 403
        assert "refused" in exc.value.message
    finally:
        srv.stop()



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



@pytest.fixture
def tty(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)


@pytest.fixture
def vm_up(fake_mp):
    C.ACCOUNTS["claude"] = {"harness": "claude"}
    fake_mp.vms["pl-sbx-t"] = {"state": "Running", "mounts": {}}
    return fake_mp


def _login(account="claude"):
    sandbox.cmd_sandbox(argparse.Namespace(action="login", account=account, yes=False))


def test_login_runs_the_harness_sign_in_inside_the_vm_and_returns_its_code(vm_up, tty):
    vm_up.login_code = 3
    with pytest.raises(SystemExit) as exc:
        _login()
    assert exc.value.code == 3
    assert vm_up.calls[-1] == ["multipass", "exec", "pl-sbx-t", "--", "claude", "/login"]
    assert vm_up.kws[-1]["capture"] is False


def test_login_touches_no_host_credential_file(vm_up, tty, fake_home, monkeypatch):
    import builtins
    import io
    from pl import harnesses
    for rel in (".claude/.credentials.json", ".codex/auth.json", ".gemini/x", ".config/gh/hosts.yml"):
        f = fake_home / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("secret")
    C.PROFILES["claude"] = fake_home / ".claude"
    opened = []
    real_builtin, real_io = builtins.open, io.open
    monkeypatch.setattr(builtins, "open", lambda f, *a, **k: (opened.append(str(f)), real_builtin(f, *a, **k))[1])
    monkeypatch.setattr(io, "open", lambda f, *a, **k: (opened.append(str(f)), real_io(f, *a, **k))[1])
    with pytest.raises(SystemExit):
        _login()
    for path in opened:
        assert not any(d in path for d in (".claude", ".codex", ".gemini", ".config/gh"))
        assert not harnesses.SECRET_RE.fullmatch(path.rsplit("/", 1)[-1])
    banned = (".claude", ".codex", ".gemini", ".config/gh", ".credentials", "auth.json")
    for argv in vm_up.calls:
        assert "transfer" not in argv and "mount" not in argv
        assert not any(b in a for a in argv for b in banned)


@pytest.mark.parametrize("which", ["stdin", "stdout"])
def test_login_without_a_terminal_refuses_and_runs_no_multipass(vm_up, monkeypatch, which):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: which != "stdin")
    monkeypatch.setattr(sys.stdout, "isatty", lambda: which != "stdout")
    with pytest.raises(SystemExit) as exc:
        _login()
    assert "needs a real terminal" in str(exc.value)
    assert vm_up.calls == []


@pytest.mark.parametrize("name", ["nobody", None])
def test_login_unknown_account_names_it_and_lists_the_accounts_and_runs_no_multipass(vm_up, tty, name):
    with pytest.raises(SystemExit) as exc:
        _login(name)
    assert repr(name) in str(exc.value) and "claude" in str(exc.value)
    assert vm_up.calls == []


def test_login_without_a_vm_says_create_first(fake_mp, tty):
    C.ACCOUNTS["claude"] = {"harness": "claude"}
    with pytest.raises(SystemExit) as exc:
        _login()
    assert "run pl sandbox create first" in str(exc.value)


def test_login_with_a_stopped_vm_says_start_first(vm_up, tty):
    vm_up.vms["pl-sbx-t"]["state"] = "Stopped"
    with pytest.raises(SystemExit) as exc:
        _login()
    assert "pl-sbx-t is Stopped: run pl sandbox start first" in str(exc.value)


def test_login_when_the_cli_is_missing_in_the_vm_names_it(vm_up, tty, monkeypatch):
    orig = vm_up._exec
    monkeypatch.setattr(vm_up, "_exec", lambda argv, kw: subprocess.CompletedProcess(argv, 1, "", "")
                        if "which" in argv else orig(argv, kw))
    with pytest.raises(SystemExit) as exc:
        _login()
    assert "claude is not installed in pl-sbx-t" in str(exc.value)
    assert "/login" not in vm_up.calls[-1]


def test_cli_sandbox_login_parses_the_account(monkeypatch):
    called = []
    monkeypatch.setattr(sandbox, "cmd_sandbox", lambda a: called.append(a))
    monkeypatch.setattr(sys, "argv", ["pl", "--profile", "t", "sandbox", "login", "claude"])
    cli.main()
    assert (called[0].action, called[0].account) == ("login", "claude")


SECRET = "s3cret-mcp-token"


@pytest.fixture
def src(tmp_path):
    f = tmp_path / "mcp.json"
    f.write_text(SECRET)
    f.chmod(0o600)
    return f


def test_copy_in_makes_a_0600_file_with_the_same_content(vm_up, src):
    sandbox.copy_in(src, "/home/ubuntu/mcp.json")
    assert vm_up.files["/home/ubuntu/mcp.json"] == [0o600, SECRET]


def test_copy_in_creates_the_file_0600_before_writing_any_content(vm_up, src):
    sandbox.copy_in(src, "/home/ubuntu/mcp.json")
    execs = [c for c in vm_up.calls if "sh" in c]
    assert execs[0][6].startswith("umask 077") and execs[1][6].startswith("cat >")
    assert vm_up.file_log[0][1:] == (0o600, "")
    assert all(mode == 0o600 for _, mode, _ in vm_up.file_log)


def test_copy_in_never_puts_the_content_in_an_argument(vm_up, src):
    sandbox.copy_in(src, "/home/ubuntu/mcp.json")
    assert not any(SECRET in a for argv in vm_up.calls for a in argv)
    assert (vm_up.inputs[-1][1]) == SECRET


def test_copy_in_to_a_missing_folder_raises_naming_vm_and_path_and_leaves_nothing(vm_up, src):
    with pytest.raises(sandbox.SandboxCopyError) as exc:
        sandbox.copy_in(src, "/nope/mcp.json")
    assert "pl-sbx-t:/nope/mcp.json" in str(exc.value)
    assert vm_up.files == {}


def test_copy_in_with_the_vm_stopped_raises_and_runs_no_exec(vm_up, src):
    vm_up.vms["pl-sbx-t"]["state"] = "Stopped"
    with pytest.raises(sandbox.SandboxCopyError) as exc:
        sandbox.copy_in(src, "/home/ubuntu/mcp.json")
    assert "pl-sbx-t:/home/ubuntu/mcp.json" in str(exc.value)
    assert not any(c[1] == "exec" for c in vm_up.calls)


def test_copy_in_when_the_write_fails_removes_the_partial_file(vm_up, src):
    vm_up.write_fails = True
    with pytest.raises(sandbox.SandboxCopyError) as exc:
        sandbox.copy_in(src, "/home/ubuntu/mcp.json")
    assert "pl-sbx-t:/home/ubuntu/mcp.json" in str(exc.value)
    assert vm_up.files == {}


def test_copy_in_refuses_a_credential_file_name(vm_up, tmp_path):
    f = tmp_path / "auth.json"
    f.write_text("x")
    with pytest.raises(sandbox.SandboxCopyError):
        sandbox.copy_in(f, "/home/ubuntu/auth.json")
    assert vm_up.calls == []


def test_whatsnew_has_the_sandbox_entry():
    from pl import whatsnew
    hits = [e for e in whatsnew.ENTRIES if "pl sandbox" in e["title"]]
    assert len(hits) == 1
    for word in ("create", "status", "start", "stop", "destroy", "login"):
        assert word in hits[0]["where"]
    assert "pl sandbox login" in hits[0]["title"]


def test_no_stage_launch_uses_the_login_or_copy_helpers():
    import inspect
    from pl import dispatch, harnesses
    for src_text in (inspect.getsource(dispatch), inspect.getsource(harnesses.launch_script)):
        for name in ("copy_in", "_login"):
            assert name not in src_text
