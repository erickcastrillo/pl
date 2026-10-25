"""--json on the read commands: one document each, the shape pinned by tests/snapshots/cli-json.json."""
import argparse
import json
import re
import time
from pathlib import Path

import pytest

from pl import alerts, board, commands, jsonout
from pl.trackers import mcp

SNAP = json.loads((Path(__file__).parent / "snapshots" / "cli-json.json").read_text())
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


def run(cmd, **kw):
    ns = {"list": dict(all=False, product=False), "card": dict(id="x", delete=False),
          "section": dict(id="x", name="spec", from_=None), "alerts": dict(all=False, ack=None)}[cmd]
    a = argparse.Namespace(**{"json": True, **ns, **kw})
    fn = {"list": commands.cmd_list, "card": commands.cmd_card, "section": commands.cmd_section,
          "alerts": alerts.cmd_alerts}[cmd]
    fn(a)


def outline(v):
    if isinstance(v, dict):
        return {k: outline(x) for k, x in v.items()}
    if isinstance(v, list):
        return [outline(v[0])] if v else []
    return type(v).__name__


@pytest.mark.parametrize("cmd", ["list", "card", "section", "alerts"])
def test_shape_snapshot(cmd, capsys):
    run(cmd)
    got = outline(json.loads(capsys.readouterr().out))
    assert got == SNAP[cmd]


@pytest.mark.parametrize("cmd", ["list", "card", "section", "alerts"])
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
    assert set(jsonout.SHAPES) == {"list", "card", "section", "alerts"}
