"""Machine-wide alerts: the GitHub budget hold and the temp folder, from fake data."""
import json
from types import SimpleNamespace

import pytest

from pl import alerts, dispatch, ghquota
from pl import config as C

NOW = 1_000_000.0


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PL_CONFIG_DIR", raising=False)
    d = tmp_path / ".pl-t"
    d.mkdir()
    (d / "config.toml").write_text("")
    C.load("t")
    monkeypatch.setattr(alerts, "_clock", lambda: NOW)
    monkeypatch.setattr(dispatch, "notify", lambda *a, **k: None)
    monkeypatch.setattr(alerts, "_offer", lambda *a: None)
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


def _fs(inodes=0.5, space=0.5):
    total = 1000
    return lambda path: SimpleNamespace(f_files=total, f_favail=int(total * (1 - inodes)),
                                        f_blocks=total, f_bavail=int(total * (1 - space)))


def _events(kind):
    p = C.STATE_DIR / "events.jsonl"
    rows = [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []
    return [r for r in rows if r["kind"] == kind]


def _hold(monkeypatch, value):
    monkeypatch.setattr(ghquota, "check", lambda kind="read": value)
    monkeypatch.setattr(dispatch, "_statvfs", _fs())


def test_gh_hold_opens_once_and_names_reset(monkeypatch):
    _hold(monkeypatch, (NOW + 3600, "GitHub GraphQL budget spent (10 of 5000 left)"))
    dispatch.check_machine_alerts()
    dispatch.check_machine_alerts()
    [a] = [a for a in alerts.listing() if a["key"] == "gh_hold"]
    assert a["severity"] == "warn" and a["count"] == 2
    assert "waits until 14:46 UTC" in a["fix"]
    assert len(_events("alert_opened")) == 1


def test_gh_hold_clears(monkeypatch):
    _hold(monkeypatch, (NOW + 3600, "spent"))
    dispatch.check_machine_alerts()
    _hold(monkeypatch, None)
    dispatch.check_machine_alerts()
    assert alerts.get("gh_hold")["resolved_at"]
    assert len(_events("alert_resolved")) == 1


def test_tmp_full_inodes_opens_high(monkeypatch):
    _hold(monkeypatch, None)
    monkeypatch.setattr(dispatch, "_statvfs", _fs(inodes=0.85, space=0.2))
    dispatch.check_machine_alerts()
    [a] = [a for a in alerts.listing() if a["key"] == "tmp_full"]
    assert a["severity"] == "high" and "inodes" in a["title"] and "85%" in a["title"]


def test_tmp_ok_resolves_and_statvfs_error_is_ignored(monkeypatch):
    _hold(monkeypatch, None)
    monkeypatch.setattr(dispatch, "_statvfs", _fs(space=0.9))
    dispatch.check_machine_alerts()
    assert alerts.get("tmp_full")
    monkeypatch.setattr(dispatch, "_statvfs", _fs(0.5, 0.5))
    dispatch.check_machine_alerts()
    assert alerts.get("tmp_full")["resolved_at"]

    def boom(path):
        raise OSError("gone")
    monkeypatch.setattr(dispatch, "_statvfs", boom)
    C.STATE_DIR.joinpath("alerts.json").unlink()
    dispatch.check_machine_alerts()
    assert alerts.get("tmp_full") is None
