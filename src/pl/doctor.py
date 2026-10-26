"""pl doctor: one list of health checks. A check returns (ok, lines); later specs add theirs to CHECKS."""
import json
from urllib.parse import urlsplit

from pl import config as C
from pl import local_model, trackers, util

NOT_A_BOARD_KEY = ("columns", "skip_tags", "repo_tags", "enabled")


def local_models():
    s = local_model.job_settings()
    host = str(s["host"])
    if why := local_model.url_problem(host, s["allow_lan"] is True):
        return False, [why]
    names = local_model.models({"url": host, "timeout": s["timeout"], "allow_lan": s["allow_lan"]})
    if names is None:
        return False, [f"Ollama: down at {host}: start it with ollama serve (or open the Ollama app)"]
    lines, ok = [f"Ollama: up, {len(names)} models"], True
    for job, model in s["models"].items():
        found = local_model.pulled(model, names)
        ok = ok and found
        lines.append(f"{job}: {model} is pulled" if found else f"{job}: {model} is not pulled: run ollama pull {model}")
    if (name := urlsplit(host).hostname) not in local_model.LOOPBACK:
        lines.append(f"warning: {name} is on your network, not this machine")
    return ok, lines


def _board(cfg):
    return {k: v for k, v in cfg.items() if k not in NOT_A_BOARD_KEY}


def tracker():
    boards = [("tracker", "[tracker]")]
    if C.INTAKE.get("type"):
        if _board(C.INTAKE) == _board(C.TRACKER):
            boards = [("tracker", "[tracker] and [intake]")]
        else:
            boards.append(("intake", "[intake]"))
    ok, lines = True, []
    for kind, label in boards:
        try:
            line = trackers.get(kind).test()
        except SystemExit as e:
            line = str(e.code if e.code is not None else "error")
        except Exception as e:
            line = f"error: {e}"
        line = util.mask(line)
        ok = ok and line.startswith("ok")
        lines.append(f"{label} {line}")
    return ok, lines


def assistant_account():
    from pl import assistant, harnesses   # imported here so doctor's import stays light
    acct, reason = assistant.pick()
    if acct is None:
        return False, ["assistant: no account can run it: set [assistant] account in config.toml to a claude or codex account"]
    return True, [f"assistant: {acct} ({harnesses.account_harness(acct).name}), {reason}"]


CHECKS = [local_models, tracker, assistant_account]


def cmd_doctor(a):
    results = [(check.__name__, *check()) for check in CHECKS]
    if getattr(a, "json", False):
        print(json.dumps({"checks": [{"name": n, "ok": ok, "lines": lines} for n, ok, lines in results]}))
    else:
        for name, ok, lines in results:
            print(f"{'ok' if ok else 'FAIL'} {name}")
            print("\n".join(f"  {ln}" for ln in lines))
    raise SystemExit(0 if all(ok for _, ok, _ in results) else 1)
