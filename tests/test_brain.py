"""The machine-wide Brain session: interactive harness session in tmux pl-brain:brain."""
import json
import stat
import subprocess

import pytest

from pl import accounts, agents, assistant, brain, manager
from pl import config as C
from test_assistant import Tmux, _script


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("PL_CONFIG_DIR", "PL_TMUX_SESSION", "PL_ASSISTANT", "PL_BRAIN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("PL_MACHINE_DIR", str(tmp_path / ".local" / "state" / "pl-machine"))
    d = tmp_path / ".pl-t"
    d.mkdir(parents=True)
    (d / "config.toml").write_text(
        '[accounts.acme]\nconfig_dir = "~/.claude-acme"\n[accounts.acme2]\nconfig_dir = "~/.claude-acme2"\n'
    )
    d2 = tmp_path / ".pl-p2"
    d2.mkdir(parents=True)
    (d2 / "config.toml").write_text(
        '[accounts.acme]\nconfig_dir = "~/.claude-acme"\n'
    )
    C.load("t")
    monkeypatch.setattr(accounts, "exhausted_profiles", lambda: set())
    monkeypatch.setattr(agents, "registry", lambda: {})
    assistant._INSTALLED.clear()
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


@pytest.fixture
def tmux(monkeypatch):
    t = Tmux()
    monkeypatch.setattr(subprocess, "run", t)
    return t


def test_two_profiles_consoles_use_the_same_brain_pane(tmux):
    status1 = brain.ensure()
    pane1 = brain.pane()
    assert pane1 is not None
    assert "brain" in status1 and "started" in status1

    # Switch console to profile p2
    C.load("p2")
    assert C.PROFILE_NAME == "p2"

    # Profile p2 connects to the same pane
    pane2 = brain.pane()
    assert pane2 == pane1
    status2 = brain.ensure()
    assert "running" in status2
    assert brain.pane() == pane1


def test_brain_state_holds_no_text_and_is_0600(tmux):
    brain.ensure()
    path = manager.machine_dir() / "brain.json"
    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    st = json.loads(path.read_text())
    allowed_keys = {"session", "session_id", "account", "harness", "window", "pane", "conversation"}
    assert set(st.keys()).issubset(allowed_keys)
    for v in st.values():
        assert isinstance(v, str)
        assert "You are" not in v and "read-only" not in v


def test_assistant_behaviour_unchanged(tmux):
    # Assistant still starts in profile's state and session
    ast = assistant.ensure()
    assert "assistant" in ast
    assert assistant.pane() is not None
    assert (C.STATE_DIR / "assistant.json").is_file()
    assert not (manager.machine_dir() / "assistant.json").exists()


def test_brain_argv_has_only_the_pl_brain_server(tmux):
    brain.ensure()
    script = _script(tmux)
    assert "--mcp-config" in script
    assert "brain-mcp.json" in script
    assert "--strict-mcp-config" in script
    assert 'mcp__pl-brain__*' in script
    assert '"deny": ["Bash", "Edit", "Write"]' in script

    # Harness without tool restriction is refused
    with pytest.raises(SystemExit) as exc:
        brain.ask_first(assistant.harnesses.get("codex"))
    assert "tool restriction" in str(exc.value)
