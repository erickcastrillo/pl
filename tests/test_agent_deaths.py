"""Antigravity's launch line takes the prompt with -i; an agent that exits with an Error: line on screen keeps that
line on the card, and a card whose agent died too often says why and what to do."""
import subprocess
import types

import pytest

from pl import agents, harnesses, watch
from pl import config as C
from pl.tui.needs import detail

ERR = ('Error: unexpected argument "/pl-run acme/app#23 review=pl:auto-review". Prompts are read '
       "only from -p/--print, -i/--prompt-interactive, or stdin, so this argument would have been ignored.")


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PL_CONFIG_DIR", raising=False)
    d = tmp_path / ".pl-t"
    d.mkdir()
    (d / "config.toml").write_text("")
    C.load("t")
    C.PROFILES = {"agy": tmp_path / "agy"}
    C.ACCOUNTS = {"agy": {"harness": "antigravity"}}
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


def _screen(monkeypatch, text, code=0):
    monkeypatch.setattr(harnesses, "_run", lambda argv, **kw: types.SimpleNamespace(returncode=code, stdout=text, stderr=""))


# ---------- the antigravity launch line ----------

def test_antigravity_gets_its_prompt_through_dash_i_after_its_mode_flag():
    h = harnesses.unattended(harnesses.get("antigravity"))
    script = harnesses.launch_script(h, "agy", "/pl-run o/r#23 review=pl:auto-review; rm -rf ~", "sid", "run:x")
    assert "exec agy --mode accept-edits -i '/pl-run o/r#23 review=pl:auto-review; rm -rf ~'\n" in script
    assert "-i" not in harnesses.launch_script(h, "agy", None, "sid", "run:x")   # no prompt: no dangling -i
    assert "-i" in harnesses.get("antigravity").note


# ---------- the death line ----------

def test_death_line_reads_the_last_error_line_on_screen(monkeypatch):
    _screen(monkeypatch, f"$ sh /x/launch.sh\n{ERR}\n\n% \n")
    assert agents.death_line("%1") == ERR[:200]
    _screen(monkeypatch, "$ sh /x/launch.sh\nbye\n% \n")
    assert agents.death_line("%1") is None
    _screen(monkeypatch, ERR, code=1)   # the pane is gone
    assert agents.death_line("%1") is None
    assert agents.death_line(None) is None


# ---------- the label of a card whose agent died too often ----------

def _died(n=3, line=ERR, stage="run"):
    w = {"stage": stage, "harness": "antigravity", "pane": "%1", "window": "@1", "profile": "agy", "attempts": n}
    m = {"pipeline_mode": "auto", "profile": "agy", "worker": w}
    if line:
        m["agent_error"] = {"stage": "run", "at": "2026-10-05T10:00:00+00:00", "line": line}
    return {"id": "card0001aaaa", "title": "A card", "list_id": "Approved", "tags": [], "updated_at": "", "metadata": m}


def test_worker_view_says_how_often_it_died_the_last_error_and_what_to_do(monkeypatch):
    monkeypatch.setattr(agents, "col_name", lambda lid: lid)
    monkeypatch.setattr(agents, "worker_status", lambda w, reg: ("dead", None))
    view = agents.worker_view(_died(), {})
    assert view.startswith("run agent died 3× · Error: unexpected argument") and view.endswith(" · pl retry")
    assert len(view) < 110
    assert agents.worker_view(_died(line=None), {}) == "run agent died 3× · pl retry"
    assert agents.worker_view(_died(stage="plan"), {}) == "plan agent died 3× · pl retry"   # an older stage's error


def test_the_pipeline_row_and_detail_say_why_and_how_to_retry(monkeypatch):
    c = _died(line="Error: [bold]x[/bold] went wrong")
    monkeypatch.setattr(watch, "cards", lambda: [c])
    monkeypatch.setattr(watch, "col_name", lambda lid: lid)
    monkeypatch.setattr(agents, "col_name", lambda lid: lid)
    monkeypatch.setattr(watch, "registry", lambda: {})
    monkeypatch.setattr(watch, "worker_status", lambda w, reg: ("dead", None))
    monkeypatch.setattr(agents, "worker_status", lambda w, reg: ("dead", None))
    monkeypatch.setattr(watch.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    monkeypatch.setattr(watch, "pr_counts", lambda: None)
    monkeypatch.setattr(watch, "paused", lambda: None)
    r = next(r for r in watch.watch_snapshot()["rows"] if r.get("card"))
    assert r["failed"] and r["kind"] == "needs"
    assert r["approved"] == "run agent died 3× · Error: [bold]x[/bold] went wrong · t to retry"
    text = detail(r).plain
    assert "Error: [bold]x[/bold] went wrong" in text   # screen text is data: plain, never markup
    assert "press t or run pl retry card0001" in text
