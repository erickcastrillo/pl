import json
from pathlib import Path
import re
import subprocess

from pl import alerts, board, events, harnesses, trackers
from pl import config as C
from pl.util import now_iso

FIELDS = {"name": 80, "company": 80, "ask": 300, "summary": 600}
MAX_ATTEMPTS = 3
CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _run(argv, **kw):
    return subprocess.run(argv, capture_output=True, text=True, **kw)


def extract_thread_link(c):
    meta = c.get("metadata") or {}
    if meta.get("thread_link"):
        return str(meta["thread_link"])
    if meta.get("thread"):
        return str(meta["thread"])
    if isinstance(meta.get("refs"), dict) and meta["refs"].get("thread"):
        return str(meta["refs"]["thread"])
    desc = c.get("description") or ""
    m = re.search(r"(?i)\bthread(?:\s+link)?:\s*(\S+)", desc)
    if m:
        return m.group(1).rstrip(")>]")
    m = re.search(r"https?://\S+", desc)
    if m:
        return m.group(0).rstrip(")>]")
    return "<link>"


def stage_connectors(stage, base=None):
    cfg = C.STAGES.get(stage) or {}
    if "connectors" in cfg:
        stage_conns = list(cfg.get("connectors") or [])
        if cfg.get("from_fields") and "email" not in stage_conns:
            return [c for c in stage_conns if c != "email"]
        return stage_conns
    conns = list(base or [])
    if cfg.get("from_fields"):
        return [c for c in conns if c != "email"]
    return conns


def clean_fields(raw):
    if not isinstance(raw, dict):
        raise ValueError("reader output must be a JSON object")
    out = {}
    for k, limit in FIELDS.items():
        if k in raw:
            v = raw[k]
            if not isinstance(v, str):
                raise ValueError(f"field {k} must be string")
            out[k] = CONTROL_CHARS.sub("", v)[:limit]
    return out


def make_mcp_config():
    p = (C.STATE_DIR or Path.home() / ".pl" / "state") / "mcp-reader.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    cfg = {
        "mcpServers": {
            "email": {
                "command": "pl",
                "args": ["connector", "proxy", "--connector", "email", "--mode", "reader"],
                "mode": "reader",
                "tools": ["get_thread"],
            }
        }
    }
    p.write_text(json.dumps(cfg, indent=2))
    return p, cfg


def run(card, stage=None, attempts=None):
    cid = card["id"] if isinstance(card, dict) else card
    c = card if isinstance(card, dict) else board.card(cid)
    meta = dict(c.get("metadata") or {})
    stage = stage or meta.get("stage") or "reader"
    h, account = harnesses.harness_for(stage, c)
    if h.name not in ("claude", "codex"):
        raise ValueError(f"reader stage supports only claude and codex, not {h.name}")

    mcp_file, mcp_cfg = make_mcp_config()
    prompt = f"/pl-read-mail {cid}"
    argv, env = harnesses.headless_argv(h, account, prompt, stage=stage)

    if h.name == "claude":
        argv = [argv[0], "--mcp-config", str(mcp_file), "--strict-mcp-config", "--tools", '""', *argv[1:]]
    elif h.name == "codex":
        argv = [argv[0], "--sandbox", "read-only", "-c", f"mcp_servers.email={json.dumps(mcp_cfg['mcpServers']['email'])}", *argv[1:]]

    res = _run(argv, env=env, cwd=C.WORK_DIR, timeout=180)
    out = (res.stdout or "").strip() if hasattr(res, "stdout") else ""
    err_msg = ""
    parsed = None
    if getattr(res, "returncode", 0) == 0 and out:
        try:
            parsed = json.loads(out)
        except Exception:
            m = re.search(r"\{[\s\S]*\}", out)
            if m:
                try:
                    parsed = json.loads(m.group(0))
                except Exception:
                    pass

    worker = dict(meta.get("worker") or {})
    att = attempts if attempts is not None else int(worker.get("attempts") or 0) + 1

    try:
        if parsed is None:
            raise ValueError(f"non-JSON reader output: {out[:120]}")
        cleaned = clean_fields(parsed)
    except Exception as e:
        err_msg = str(e)
        w = {**worker, "attempts": att, "stage": stage, "agent_error": {"stage": stage, "at": now_iso(), "line": err_msg}}
        board.update(cid, metadata={"worker": w})
        events.emit("agent_died", cid, stage=stage, attempts=att)
        if att >= MAX_ATTEMPTS:
            alerts.open(f"stage_failed:{cid}:{stage}", "high", f"Card {cid}: reader agent failed {att} times", "pl retry")
        return False

    parts = board.sections(c.get("description") or "")
    parts["FIELDS"] = "\n".join(f"{k}: {v}" for k, v in cleaned.items())
    new_desc = board.render(parts)
    new_meta = {**meta, "fields": cleaned, "worker": None}
    board.update(cid, description=board.check_size(new_desc), metadata=new_meta)

    next_col = (C.STAGES.get(stage) or {}).get("next") or (C.STAGES.get(stage) or {}).get("done_at") or C.STAGE_DONE_AT.get(stage) or "Spec ready"
    t = trackers.get("tracker")
    if hasattr(t, "move"):
        t.move(cid, next_col)
    elif hasattr(t, "update"):
        t.update(cid, column=next_col)
    return True
