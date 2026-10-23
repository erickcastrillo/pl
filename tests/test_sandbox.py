"""Tests for the Multipass sandbox VM lifecycle and config."""
import pytest

from pl import config as C
from pl import sandbox


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("PL_CONFIG_DIR", "PL_TMUX_SESSION"):
        monkeypatch.delenv(var, raising=False)
    d = tmp_path / ".pl-t"
    d.mkdir()
    (d / "config.toml").write_text("")
    C.load("t")
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


def test_sandbox_settings_default_off():
    assert C.SANDBOX == {}
    assert sandbox.settings() == sandbox.DEFAULTS


def test_config_reads_the_sandbox_table(fake_home):
    (fake_home / ".pl-t" / "config.toml").write_text('[sandbox]\nenabled = true\ncpus = 4\nmemory = "8G"\n')
    C.load("t")
    assert sandbox.settings() == {"enabled": True, "cpus": 4, "memory": "8G", "disk": "20G"}


def test_validate_checks_the_sandbox_table():
    good = {"sandbox": {"enabled": True, "cpus": 4, "memory": "8G", "disk": "30G"}}
    assert C.validate(good) == []

    bad_cases = [
        ({"enabled": "yes"}, "sandbox.enabled must be true or false"),
        ({"cpus": 0}, "sandbox.cpus must be a whole number of at least 1"),
        ({"cpus": True}, "sandbox.cpus must be a whole number of at least 1"),
        ({"memory": 4}, 'sandbox.memory must be a size like "4G" or "512M"'),
        ({"disk": "lots"}, 'sandbox.disk must be a size like "4G" or "512M"'),
        ({"image": "x"}, "sandbox.image is not a setting; the keys are enabled, cpus, memory, disk"),
    ]
    for sb, expected_err in bad_cases:
        errs = C.validate({"sandbox": sb})
        assert any(expected_err in e for e in errs), f"Expected {expected_err!r} in {errs}"
