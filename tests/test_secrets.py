"""Plan 11 slice 2: connector tokens live in the OS keychain, or in a 0600 file when there is none."""
import base64
import json
import os
import stat
import subprocess

import pytest

from pl import secrets, util
from pl.trackers import mcp

DATA = {"access_token": "at-s3cret-\"quoted\" value", "refresh_token": "rt-s3cret", "expires_at": 1893456000}


class FakeKeychain:
    """An in-memory `security` / `secret-tool` that records every argv and stdin it is given."""

    def __init__(self):
        self.items, self.calls = {}, []

    def __call__(self, argv, input=None):
        self.calls.append((list(argv), input))
        tool = argv[0]
        if tool == "security" and argv[1] == "-i":
            words = input.split()
            self.items[words[words.index("-a") + 1]] = words[words.index("-w") + 1]
            return subprocess.CompletedProcess(argv, 0, "", "")
        if tool == "security":
            name = argv[argv.index("-a") + 1]
            if argv[1] == "delete-generic-password":
                return subprocess.CompletedProcess(argv, 0 if self.items.pop(name, None) else 44, "", "")
            found = self.items.get(name)
            return subprocess.CompletedProcess(argv, 0 if found else 44, (found or "") + "\n", "")
        name = argv[argv.index("account") + 1]
        if argv[1] == "store":
            self.items[name] = input
        elif argv[1] == "clear":
            self.items.pop(name, None)
        elif argv[1] == "lookup":
            return subprocess.CompletedProcess(argv, 0 if name in self.items else 1, self.items.get(name, ""), "")
        return subprocess.CompletedProcess(argv, 0, "", "")


@pytest.fixture(autouse=True)
def clean(monkeypatch, tmp_path):
    monkeypatch.setenv("PL_MACHINE_DIR", str(tmp_path / "machine"))
    monkeypatch.delenv("PL_KEYCHAIN", raising=False)
    monkeypatch.setattr(secrets.shutil, "which", lambda tool: None)
    monkeypatch.setattr(secrets, "_run", lambda argv, input=None: pytest.fail(f"the real {argv[0]} was run"))
    before = set(mcp._secrets)
    yield
    mcp._secrets.intersection_update(before)


def keychain(monkeypatch, platform):
    fake = FakeKeychain()
    tool = "security" if platform == "darwin" else "secret-tool"
    monkeypatch.setattr(secrets.sys, "platform", platform)
    monkeypatch.setattr(secrets.shutil, "which", lambda t: f"/usr/bin/{t}" if t == tool else None)
    monkeypatch.setattr(secrets, "_run", fake)
    return fake


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_keychain_put_never_puts_the_secret_in_argv(monkeypatch, platform):
    fake = keychain(monkeypatch, platform)
    secrets.put("billing", DATA)
    assert secrets.backend() == "keychain"
    for argv, _ in fake.calls:
        assert not any(v in " ".join(argv) for v in ("at-s3cret", "rt-s3cret"))
    stored = fake.items["billing"]
    assert "at-s3cret" not in stored
    assert json.loads(base64.b64decode(stored)) == DATA
    assert secrets.get("billing") == DATA


def test_no_keychain_falls_back_to_a_0600_file_in_a_0700_folder(tmp_path):
    secrets.put("billing", DATA)
    folder = tmp_path / "machine" / "secrets"
    assert secrets.backend() == "file"
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert stat.S_IMODE((folder / "billing.json").stat().st_mode) == 0o600
    assert [p.name for p in folder.iterdir()] == ["billing.json"]
    assert secrets.get("billing") == DATA
    assert secrets.get("missing") is None


def test_pl_keychain_off_forces_the_file(monkeypatch, tmp_path):
    fake = keychain(monkeypatch, "linux")
    monkeypatch.setenv("PL_KEYCHAIN", "off")
    secrets.put("billing", DATA)
    assert fake.calls == [] and (tmp_path / "machine" / "secrets" / "billing.json").exists()


def test_a_symlinked_secret_file_is_refused(tmp_path):
    folder = tmp_path / "machine" / "secrets"
    folder.mkdir(parents=True, mode=0o700)
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps(DATA))
    os.symlink(target, folder / "billing.json")
    with pytest.raises(SystemExit):
        secrets.get("billing")
    with pytest.raises(SystemExit):
        secrets.put("billing", {"access_token": "new"})
    assert json.loads(target.read_text()) == DATA


def test_loaded_tokens_are_masked_by_util_mask(monkeypatch):
    keychain(monkeypatch, "linux")
    secrets.put("billing", DATA)
    mcp._secrets.difference_update(DATA.values())
    secrets.get("billing")
    shown = util.mask(f"calling with {DATA['access_token']} and {DATA['refresh_token']}")
    assert "s3cret" not in shown


def test_delete_removes_from_both_backends(monkeypatch, tmp_path):
    secrets.put("billing", DATA)
    fake = keychain(monkeypatch, "darwin")
    secrets.put("billing", DATA)
    secrets.delete("billing")
    assert fake.items == {} and not (tmp_path / "machine" / "secrets" / "billing.json").exists()
    secrets.delete("billing")


@pytest.mark.parametrize("name", ["", "../x", "a b", "a;b", "-a"])
def test_a_connector_name_that_is_not_plain_is_refused(name):
    with pytest.raises(ValueError):
        secrets.put(name, DATA)
