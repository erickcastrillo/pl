"""pod.md versions, rule add and rollback. Temp profile folder; nothing real is touched."""
import argparse
import json

import pytest

from pl import cli, pods
from pl import config as C


@pytest.fixture
def profile(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "CONFIG_DIR", tmp_path)
    return tmp_path


def args(**kw):
    base = dict(pods_cmd=None, pod="billing", text=None, yes=True, note=None, to=None, json=False)
    return argparse.Namespace(**{**base, **kw})


def test_each_save_is_a_numbered_version(profile):
    pods.save_version("billing", "one\n", "first", "me")
    pods.save_version("billing", "two\n", "second", "me")
    versions = pods.history("billing")
    assert [v["n"] for v in versions] == [1, 2]
    assert versions[1]["note"] == "second" and versions[1]["author"] == "me"
    assert (profile / "pods" / ".history" / "billing" / "2.md").read_text() == "two\n"
    assert pods.pod_file("billing").read_text() == "two\n"
    assert (pods.pod_file("billing").stat().st_mode & 0o777) == 0o600


def test_a_hand_edit_is_kept_as_a_version_before_the_next_change(profile):
    pods.save_version("billing", "one\n", "first", "me")
    pods.pod_file("billing").write_text("hand edit\n")
    pods.save_version("billing", "two\n", "second", "me")
    versions = pods.history("billing")
    assert [v["note"] for v in versions] == ["first", "edited outside pl", "second"]
    assert (profile / "pods" / ".history" / "billing" / "2.md").read_text() == "hand edit\n"


def test_rule_add_adds_the_rule_as_a_new_version(profile, capsys):
    pods.save_version("sales", "# Sales\n", "start", "me")
    assert pods.cmd_pods(args(pods_cmd="rule", pod="sales", text="Never offer discounts")) == 0
    text = pods.pod_file("sales").read_text()
    assert "## Rules\n- Never offer discounts\n" in text
    assert len(pods.history("sales")) == 2


def test_rollback_to_2_matches_version_2_exactly_and_is_a_new_version(profile):
    for text in ("one\n", "two\n", "three\n"):
        pods.save_version("billing", text, "edit", "me")
    assert pods.cmd_pods(args(pods_cmd="rollback", to=2)) == 0
    assert pods.pod_file("billing").read_bytes() == b"two\n"
    versions = pods.history("billing")
    assert len(versions) == 4 and versions[-1]["note"] == "rollback to 2"


def test_rollback_to_an_unknown_version_exits_2(profile):
    pods.save_version("billing", "one\n", "first", "me")
    with pytest.raises(SystemExit) as e:
        pods.cmd_pods(args(pods_cmd="rollback", to=9))
    assert e.value.code == 2


def test_without_a_terminal_and_without_yes_exits_2(profile, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(SystemExit) as e:
        pods.cmd_pods(args(pods_cmd="rule", text="x", yes=False))
    assert e.value.code == 2
    assert not pods.pod_file("billing").exists()


def test_history_json(profile, capsys):
    pods.save_version("billing", "one\n", "first", "me")
    pods.cmd_pods(args(pods_cmd="history", json=True))
    assert json.loads(capsys.readouterr().out)[0]["n"] == 1


def test_pods_is_a_cli_command():
    assert cli.pods is pods
