"""pl e2e console: run the console acceptance suite (tests/e2e_ui) from a pl checkout and write report.md."""
import argparse
import importlib.metadata
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from pl import alerts, board, checkout_watch, ghquota, trackers, update, util
from pl import config as C

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


def _gh(argv, env=None):
    got = _run(argv, env=env, capture=True) or (124, "timed out")
    return subprocess.CompletedProcess(argv, got[0], got[1], "")


def _rate():
    got = ghquota.gh(_gh, ["api", "rate_limit"], env=C.gh_env())
    if got.returncode != 0:
        raise RuntimeError(f"cannot read the GitHub rate limit: {got.stdout.strip()}")
    return json.loads(got.stdout)["resources"]["graphql"]


def _board():
    return board.cards()


def _check_version():
    got = _run(["pl", "--version"], env={**os.environ, "PL_NO_UPDATE_CHECK": "1"}, capture=True)
    on_path = (got[1].splitlines() or [""])[0] if got and got[0] == 0 else "unreadable"
    installed = importlib.metadata.version("pl")
    if on_path == f"pl {update.__version__}" and installed == update.__version__:
        return "pass", f"pl {update.__version__}"
    return "fail", f"console shows {update.__version__}, installed {installed}, pl on PATH {on_path.removeprefix('pl ')}"


def _check_points():
    if not str(C.TRACKER.get("type") or "").startswith("github"):
        return "n/a", "not a GitHub profile"
    before = _rate()
    board.fresh_next()
    board.cards()
    after = _rate()
    spent, limit = before["remaining"] - after["remaining"], before["remaining"] // 10
    return ("pass" if spent <= limit else "fail"), f"spent {spent} of {limit} allowed ({before['remaining']} left this hour)"


def _check_alerts():
    bad = [a for a in alerts.listing() if a["key"].split(":", 1)[0] in ("stalled", "stage_failed")]
    if not bad:
        return "pass", "no stalled or dead agent"
    return "fail", ", ".join(c for a in bad for c in (a.get("cards") or [a["key"]]))


def _check_done_windows():
    got = _run(["tmux", "list-windows", "-t", C.TMUX_SESSION, "-F", "#{window_id}"], capture=True)
    if not got or got[0] != 0:
        return "pass", "no tmux session"
    open_windows = set(got[1].split())
    stuck = [(c["id"], w) for c in _board() if board.col_name(c["list_id"]) == "Done"
             and (w := ((c.get("metadata") or {}).get("worker") or {}).get("window")) in open_windows]
    if stuck:
        return "fail", ", ".join(f"{cid} has window {w}" for cid, w in stuck)
    return "pass", f"{len(open_windows)} windows, none on a Done card"


def _check_checkout():
    if not C.WORK_DIR:
        return "n/a", "no repository folder set"
    paths = checkout_watch.dirty_paths()
    if paths is None:
        return "fail", f"cannot read git status in {C.WORK_DIR}"
    if paths:
        return "fail", f"{len(paths)} changed: {', '.join(sorted(paths)[:5])}"
    return "pass", f"{C.WORK_DIR} is clean"


def _check_vikunja():
    if C.TRACKER.get("type") != "vikunja":
        return "n/a", "not a Vikunja profile"
    t = trackers.get("tracker")
    total = sum(b.get("count") or 0 for b in t._http("GET", f"{t._view_path()}/buckets"))
    shown = len(_board())
    if total <= 50:
        return "pass", f"board has {total} cards"
    return ("fail" if shown < total else "pass"), (f"pl list --all shows {shown} of {total}" if shown < total else f"{shown} of {total}")


CHECKS = (("a", "console version", _check_version), ("b", "GitHub points for one board read", _check_points),
          ("c", "no stalled or dead agent alert", _check_alerts), ("d", "no agent window on a Done card", _check_done_windows),
          ("e", "main checkout clean", _check_checkout), ("f", "pl list --all shows every Vikunja card", _check_vikunja))


def live(name):
    if not name:
        print("pl e2e console: --live needs --profile NAME", file=sys.stderr)
        return 2
    if not C.NAME_RE.match(name) or not (C.HOME / f".pl-{name}" / "config.toml").is_file():
        print(f"pl e2e console: unknown profile {name!r}", file=sys.stderr)
        return 2
    C.load(name)
    tally = {"pass": 0, "fail": 0, "n/a": 0}
    for key, title, check in CHECKS:
        try:
            status, reason = check()
        except (SystemExit, Exception) as e:
            status, reason = "fail", util.mask(str(e)) or type(e).__name__
        tally[status] += 1
        print(f"{key}. {status:5} {title}: {util.mask(reason)}")
    print(f"{tally['pass']} passed, {tally['fail']} failed, {tally['n/a']} not applicable")
    return 1 if tally["fail"] else 0


def cmd(argv):
    ap = argparse.ArgumentParser(prog="pl e2e console", description="Run the console acceptance suite (tests/e2e_ui) "
                                 "against a throwaway profile. Exit 0 no item failed, 1 an item failed or the run timed "
                                 "out, 2 the console could not start or this is not a pl checkout.")
    ap.add_argument("--out", help="a folder for report.md and screens/ (default ~/pl-e2e/console/<timestamp>)")
    ap.add_argument("--timeout", type=int, default=600, help="seconds before the run is stopped (default 600)")
    ap.add_argument("--live", action="store_true", help="check a real profile, read only, instead of running the suite")
    ap.add_argument("--profile", help="with --live: the real profile to check, read only")
    a = ap.parse_args(argv)
    if a.profile and not a.live:
        ap.error("--profile goes with --live")
    if a.live:
        return live(a.profile)
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
