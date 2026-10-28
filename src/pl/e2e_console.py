"""pl e2e console: run the console acceptance suite (tests/e2e_ui) from a pl checkout and write report.md."""
import argparse
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

ITEMS = tuple(str(n) for n in range(1, 17)) + tuple(f"16{c}" for c in "abcdefghijklmnopq")
TABS = ("dashboard", "assistant", "work", "review", "automate", "extend", "activity", "settings", "home")
DROPPED_ENV = ("PL_CONFIG_DIR", "PL_PROFILE", "PL_WORKTREES_DIR")
PYTEST = ["uv", "run", "pytest", "-m", "e2e_ui", "-q", "-p", "no:cacheprovider", "tests/e2e_ui"]


def _run(argv, cwd=None, env=None, timeout=60, capture=False):
    try:
        r = subprocess.run(argv, cwd=cwd, env=env, timeout=timeout, text=True, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE if capture else None)
    except subprocess.TimeoutExpired:
        return None
    except OSError as e:
        return 127, str(e)
    return r.returncode, r.stdout or ""


def _lines(out):
    path = Path(out) / "results.jsonl"
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def _verdict(rows, timed_out):
    if not rows:
        return "fail: timed out" if timed_out else "fail: no test for this item", None
    bad = next((r for r in rows if r["outcome"] in ("failed", "error")), None)
    if bad:
        return f"fail: {bad['reason']}", bad
    if all(r["outcome"] == "skipped" for r in rows):
        return rows[0]["reason"] if rows[0]["reason"].startswith("not testable here") else f"not testable here: {rows[0]['reason']}", None
    return "pass", None


def _where(bad):
    if not bad:
        return ""
    w = bad.get("where") or {}
    shot = f" [screenshot]({bad['screen']})" if bad.get("screen") else ""
    return f"{w.get('tab', '')} / {w.get('screen', '')} / `{w.get('key', '')}`{shot}"


def _table(names, lines, field, timed_out):
    rows, failed = [], 0
    for name in names:
        verdict, bad = _verdict([x for x in lines if x.get(field) == name], timed_out)
        failed += verdict.startswith("fail")
        rows.append(f"| {name} | {verdict} | {_where(bad)} |")
    return ["| " + field + " | result | where |", "|---|---|---|", *rows], failed


def report(out, timed_out):
    lines = _lines(out)
    items, bad_items = _table(ITEMS, lines, "item", timed_out)
    tabs, bad_tabs = _table(TABS, lines, "tab", timed_out)
    text = "\n".join(["# Console acceptance report", "", *items, "", "## Console tabs", "", *tabs, ""])
    (Path(out) / "report.md").write_text(text)
    return bad_items + bad_tabs, lines


def cmd(argv):
    ap = argparse.ArgumentParser(prog="pl e2e console", description="Run the console acceptance suite (tests/e2e_ui) "
                                 "against a throwaway profile. Exit 0 no item failed, 1 an item failed or the run timed "
                                 "out, 2 the console could not start or this is not a pl checkout.")
    ap.add_argument("--out", help="a folder for report.md and screens/ (default ~/pl-e2e/console/<timestamp>)")
    ap.add_argument("--timeout", type=int, default=600, help="seconds before the run is stopped (default 600)")
    a = ap.parse_args(argv)
    top = _run(["git", "rev-parse", "--show-toplevel"], capture=True)
    root = Path(top[1].strip()) if top and top[0] == 0 else None
    if not root or not (root / "tests" / "e2e_ui").is_dir():
        print("pl e2e console: run it from a pl checkout (no tests/e2e_ui)")
        return 2
    out = Path(a.out) if a.out else Path.home() / "pl-e2e" / "console" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in DROPPED_ENV}
    env.update(PL_E2E_UI_OUT=str(out), PL_NO_UPDATE_CHECK="1")
    ran = _run(PYTEST, cwd=str(root), env=env, timeout=a.timeout)
    failed, lines = report(out, ran is None)
    print(f"report: {out / 'report.md'} ({failed} failed of {len(ITEMS) + len(TABS)})")
    if (ran and ran[0] in (2, 3, 4, 5)) or any(x.get("item") == "start" for x in lines):
        return 2
    return 1 if ran is None or failed else 0
