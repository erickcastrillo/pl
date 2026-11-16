"""pl steward --once prints one line per card it looked at."""
import json
from argparse import Namespace

from pl import steward

ROWS = [{"card": "o/r#1", "column": "In progress", "reason": "blocked", "action": "none", "by": "rule"}]


def _run(monkeypatch, capsys, **kw):
    monkeypatch.setattr(steward.board, "cards", lambda: [])
    monkeypatch.setattr(steward, "load_state", lambda: {})
    monkeypatch.setattr(steward, "save_state", lambda st: None)
    monkeypatch.setattr(steward, "run_pass", lambda cards, st, act=True, **k: ROWS)
    steward.cmd_steward(Namespace(**{"once": True, "dry_run": False, "json": False, **kw}))
    return capsys.readouterr().out


def test_once_prints_id_column_reason_action(monkeypatch, capsys):
    out = _run(monkeypatch, capsys)
    assert out.split() == ["r#1", "In", "progress", "blocked", "none"]


def test_once_json(monkeypatch, capsys):
    assert json.loads(_run(monkeypatch, capsys, json=True)) == {"rows": ROWS}
