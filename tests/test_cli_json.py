"""--json on the read commands: one document each, the shape pinned by tests/snapshots/cli-json.json."""
import argparse
import json
import re
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pl import alerts, board, commands, events, jsonout, profiles, standup, usage, watch
from pl import config as C
from pl.trackers import mcp

SNAP = json.loads((Path(__file__).parent / "snapshots" / "cli-json.json").read_text())
PR = "https://github.com/acme/app/pull/3"
NOW = time.time()
ISO = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(NOW - 120))
CARD = {"id": "acme/app#7", "title": "A card", "list_id": "L1", "tags": ["idea"], "updated_at": ISO,
        "metadata": {"pipeline_mode": "auto"}, "description": "# PIPELINE: SPEC\nthe spec\n"}


@pytest.fixture(autouse=True)
def fake_board(monkeypatch):
    monkeypatch.setattr(commands, "col_name", lambda lid: "Spec ready")
    monkeypatch.setattr(commands, "cards", lambda: [CARD])
    monkeypatch.setattr(commands, "find_card", lambda cid: CARD)
    monkeypatch.setattr(commands, "registry", lambda: {})
    monkeypatch.setattr(commands, "share", lambda *a: None)
    monkeypatch.setattr(commands, "worker_view", lambda c, reg: "manual")
    monkeypatch.setattr(commands, "approved_label", lambda c, col, reg: None)
    monkeypatch.setattr(commands, "exhausted_profiles", lambda: [])
    monkeypatch.setattr(commands, "paused", lambda: None)
    monkeypatch.setattr(commands, "intake_configured", lambda: False)
    monkeypatch.setattr(commands, "sections", lambda d: board.sections(d))
    monkeypatch.setattr(alerts, "listing", lambda all=False: [
        {"key": "k1", "severity": "high", "title": "t", "fix": "f", "first_seen": 1.5, "count": 2}])


PLAN_CARD = {"id": "acme/app#9", "title": "Planned", "list_id": "L1", "metadata": {"pr_urls": [PR]},
             "description": "# PIPELINE: SPEC\nthe spec\n\n# PIPELINE: PLAN\n## Summary\nx\n\n## Minimum change\nsmall\n"}


@pytest.fixture(autouse=True)
def fake_reads(monkeypatch):
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(usage, "scan", lambda: {})
    monkeypatch.setattr(C, "USAGE", {"prices": {"m": [1, 1]}})
    monkeypatch.setattr(usage, "totals", lambda *a, **k: {"work": [1, 2, 3, 4, 0.5, False]})
    monkeypatch.setattr(standup, "pr_summary", lambda start: {k: {"total": 1, "items": [{"repo": "app", "number": 3, "title": "t", "url": "u", "yours": True}]}
                                                              for k in ("merged", "opened", "closed")})
    monkeypatch.setattr(standup, "_cards", lambda: [])
    monkeypatch.setattr(watch, "watch_snapshot", lambda: {"rows": [], "needs": {}})
    monkeypatch.setattr(events, "_read", lambda: [{"kind": "idea_approved", "card": "c1", "_t": now - timedelta(hours=1)},
                                                  {"kind": "error", "message": "boom", "_t": now - timedelta(hours=1)}])
    monkeypatch.setattr(C, "PROFILES", {"work": "/home/x/.claude-work"})
    monkeypatch.setattr(commands, "exhausted_profiles", lambda: ["work"])
    monkeypatch.setattr(commands, "parking", lambda p: {"until": "2026-01-01T10:00:00", "reason": "limit", "exhausted_at": "2026-01-01T08:00:00"})
    monkeypatch.setattr(profiles, "list_profiles", lambda check_lock=True: [
        {"name": "one", "dir": "/home/x/.pl-one", "running": False, "holder": "", "tmux_session": "pl-one", "tracker": "github 1",
         "board_id": "1", "accounts": {"work": "/home/x/.claude-work"}}])
    monkeypatch.setattr(commands, "cards", lambda: [PLAN_CARD])
    monkeypatch.setattr(commands, "card", lambda cid: PLAN_CARD)
    monkeypatch.setattr(commands.subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=0, stdout=json.dumps({"body": ""})))


NS = {"list": dict(all=False, product=False), "card": dict(id="x", delete=False),
      "section": dict(id="x", name="spec", from_=None), "alerts": dict(all=False, ack=None),
      "usage": dict(since=None, by="account", github=False),
      "standup": dict(since=None, markdown=False, slack=False, summary=False),
      "accounts": dict(reset=None), "profiles": dict(profiles_cmd=None), "intent": dict(pr=PR)}
FN = {"list": commands.cmd_list, "card": commands.cmd_card, "section": commands.cmd_section, "alerts": alerts.cmd_alerts,
      "usage": usage.cmd_usage, "standup": standup.cmd_standup, "accounts": commands.cmd_profiles,
      "profiles": profiles.cmd_profiles, "intent": commands.cmd_intent}
ALL = list(FN)


def run(cmd, **kw):
    FN[cmd](argparse.Namespace(**{"json": True, **NS[cmd], **kw}))


def outline(v):
    if isinstance(v, dict):
        return {k: outline(x) for k, x in v.items()}
    if isinstance(v, list):
        return [outline(v[0])] if v else []
    return type(v).__name__


@pytest.mark.parametrize("cmd", ALL)
def test_shape_snapshot(cmd, capsys):
    run(cmd)
    got = outline(json.loads(capsys.readouterr().out))
    assert got == SNAP[cmd]


@pytest.mark.parametrize("cmd", ALL)
def test_every_json_output_is_one_document(cmd, capsys):
    run(cmd)
    out = capsys.readouterr().out
    assert out.endswith("\n") and out.count("\n") == 1
    json.loads(out)


def test_list_json_skips_status_banners(capsys, monkeypatch):
    monkeypatch.setattr(commands, "paused", lambda: {"since": "2026-01-01T00:00"})
    monkeypatch.setattr(commands, "exhausted_profiles", lambda: ["a"])
    run("list")
    json.loads(capsys.readouterr().out)


def test_json_output_is_masked(capsys, monkeypatch):
    monkeypatch.setattr(mcp, "_secrets", {"s3cr3t-value"})
    monkeypatch.setitem(CARD, "title", "leaks s3cr3t-value")
    run("card")
    out = capsys.readouterr().out
    assert "s3cr3t-value" not in out and "***" in out


def test_section_json_has_card_name_and_text(capsys):
    run("section")
    assert json.loads(capsys.readouterr().out) == {"card": "acme/app#7", "name": "SPEC", "text": "the spec"}


@pytest.mark.parametrize("as_json", [True, False])
def test_a_missing_section_exits_1_with_the_reason(as_json, capsys):
    with pytest.raises(SystemExit) as e:
        run("section", name="design", json=as_json)
    assert isinstance(e.value.code, str) and "no DESIGN section" in e.value.code
    assert capsys.readouterr().out == ""


def test_docs_cli_json_names_every_shape_in_SHAPES():
    doc = (Path(__file__).parent.parent / "docs" / "cli-json.md").read_text()
    for cmd, shape in jsonout.SHAPES.items():
        assert re.search(rf"^## {re.escape(shape)}\b", doc, re.M), f"{cmd}: no '## {shape}' in docs/cli-json.md"
    assert set(jsonout.SHAPES) == set(ALL)


def test_usage_json_rows_are_the_table_data(capsys):
    run("usage")
    got = json.loads(capsys.readouterr().out)
    assert got["by"] == "account" and got["usage"]["work"] == {"input": 1, "output": 2, "cache_read": 3, "cache_write": 4,
                                                             "cost": 0.5, "unpriced": False}


def test_accounts_json_says_who_is_parked(capsys):
    run("accounts")
    assert json.loads(capsys.readouterr().out)["accounts"][0]["parked"] is True


def test_intent_json_keeps_the_plan_scope_sections_only(capsys):
    run("intent")
    card = json.loads(capsys.readouterr().out)["cards"][0]
    assert card["spec"] == "the spec" and card["plan_scope"].startswith("## Minimum change") and "Summary" not in card["plan_scope"]
