"""pl notify test: one test notification through attention_cmd and, with --chat, one connector."""
import argparse
import subprocess

import pytest
import tomlkit

from pl import chat, notifications, trackers
from pl import config as C
from pl.notifications import TEST_MESSAGE, TEST_TITLE


class FakeAdapter:
    sent: list = []
    fail = None

    def __init__(self, name, cfg):
        self.name, self.cfg = name, cfg

    def send(self, chat_id, text, buttons=None):
        if FakeAdapter.fail is not None:
            if isinstance(FakeAdapter.fail, BaseException):
                raise FakeAdapter.fail
            return FakeAdapter.fail
        FakeAdapter.sent.append((str(chat_id), text))
        return {"ok": True}


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    saved = C.ATTENTION
    mdir = tmp_path / "machine"
    mdir.mkdir()
    monkeypatch.setenv("PL_MACHINE_DIR", str(mdir))
    monkeypatch.setenv("HOME", str(tmp_path))
    doc = tomlkit.document()
    conns = tomlkit.table()
    conns["fake"] = {"role": "messaging", "adapter": "fake", "token": "${FAKE_TOKEN}", "allow": ["111", "222"]}
    doc["connectors"] = conns
    (mdir / "machine.toml").write_text(tomlkit.dumps(doc))
    monkeypatch.setitem(chat.ADAPTERS, "fake", FakeAdapter)
    FakeAdapter.sent, FakeAdapter.fail = [], None
    script = tmp_path / "attn.sh"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    C.ATTENTION = script
    monkeypatch.setattr(trackers, "_dry", False)
    yield script
    C.ATTENTION = saved


class Calls(list):
    rc: dict


@pytest.fixture
def runs(monkeypatch):
    calls = Calls()
    code = {"rc": 0}

    def fake(argv):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, code["rc"], b"", b"")
    monkeypatch.setattr(notifications, "_run", fake)
    calls.rc = code
    return calls


def notify(capsys, chat_name=None):
    with pytest.raises(SystemExit) as e:
        notifications.cmd_notify(argparse.Namespace(action="test", chat=chat_name))
    out = capsys.readouterr()
    return e.value.code, out.out + out.err


def test_runs_attention_once_and_prints_what_it_sent(capsys, runs, env):
    code, out = notify(capsys)
    assert code == 0
    assert runs == [[str(env), "notify", TEST_TITLE, TEST_MESSAGE]]
    assert TEST_TITLE in out and TEST_MESSAGE in out and "attention_cmd: sent" in out


def test_chat_name_also_receives_the_same_text_once(capsys, runs):
    code, out = notify(capsys, "fake")
    assert code == 0
    assert FakeAdapter.sent == [("111", f"{TEST_TITLE}: {TEST_MESSAGE}"), ("222", f"{TEST_TITLE}: {TEST_MESSAGE}")]
    assert "fake: sent" in out


@pytest.mark.parametrize("case", ["unset", "missing", "not_executable"])
def test_attention_not_set_or_not_executable_fails(capsys, runs, env, tmp_path, case):
    if case == "unset":
        C.ATTENTION = None
    elif case == "missing":
        C.ATTENTION = tmp_path / "nope.sh"
    else:
        env.chmod(0o644)
    code, out = notify(capsys)
    assert code == 1
    assert "attention_cmd: failed" in out
    assert (str(C.ATTENTION) in out) if case != "unset" else "not set" in out
    assert runs == []


def test_attention_non_zero_exit_is_reported(capsys, runs):
    runs.rc["rc"] = 3
    code, out = notify(capsys)
    assert code == 1 and "attention_cmd: failed: exit 3" in out


def test_unknown_chat_name_sends_nothing(capsys, runs):
    code, out = notify(capsys, "ghost")
    assert code == 1 and "ghost" in out and "not a configured chat connector" in out
    assert runs == [] and FakeAdapter.sent == []


@pytest.mark.parametrize("fail", [RuntimeError("bot123:SECRETTOKEN"), {"ok": False, "description": "SECRETTOKEN"}])
def test_chat_send_failure_is_reported_without_secrets(capsys, runs, fail):
    FakeAdapter.fail = fail
    code, out = notify(capsys, "fake")
    assert code == 1
    assert "fake: failed" in out and "attention_cmd: sent" in out and "SECRETTOKEN" not in out


def test_chat_still_sent_when_attention_missing(capsys, runs):
    C.ATTENTION = None
    code, out = notify(capsys, "fake")
    assert code == 1
    assert len(FakeAdapter.sent) == 2 and "attention_cmd: failed" in out and "fake: sent" in out


def test_dry_run_sends_nothing_and_exits_0(capsys, runs, monkeypatch):
    monkeypatch.setattr(trackers, "_dry", True)
    code, out = notify(capsys, "fake")
    assert code == 0 and "dry-run: nothing sent" in out
    assert runs == [] and FakeAdapter.sent == []
