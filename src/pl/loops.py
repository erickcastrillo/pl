"""pl loops: list|add|pause|resume|rm manage [loops.<name>] in the profile's config.toml; run queues a run now."""
import json
import sys
import time
from datetime import datetime

import tomlkit

from pl import config as C
from pl import schedule
from pl.util import BadInput, ask, load_state, save_state

WAIT_POLLS = 150


def _fail(message: str):
    print(f"pl loops: {message}", file=sys.stderr)
    raise SystemExit(2)


def _confirm(question: str, yes: bool):
    if yes:
        return
    try:
        answer = ask("loops", f"{question} [y/N]: ")
    except EOFError:
        answer = ""
    if answer.strip().lower() not in ("y", "yes"):
        print("nothing changed")
        raise SystemExit(0)


def _document():
    path = C.path()
    if path is None:
        raise SystemExit(C.READ_ONLY)
    return tomlkit.parse(path.read_text())


def _save(doc):
    C.save(doc)
    C.load(config_dir=str(C.CONFIG_DIR))


def _row(name: str, loop: dict, paused: bool, now: datetime) -> dict:
    every = loop.get("every")
    next_run = None
    if every and not paused:
        next_run = schedule.next_run(schedule.parse_every(every), now).isoformat(timespec="minutes")
    return {"name": name, "state": "paused" if paused else "on", "every": every, "next_run": next_run,
            "account": loop.get("account") or loop.get("profile"), "skill": loop.get("skill"), "prompt": loop.get("prompt")}


def _rows() -> list[dict]:
    now = datetime.now()
    on = [_row(n, v, False, now) for n, v in C.SERVICES.items()]
    off = [_row(n, v, True, now) for n, v in C.LOOPS_OFF.items()]
    return sorted(on + off, key=lambda r: r["name"])


def _cmd_list(a) -> int:
    rows = _rows()
    if a.json:
        print(json.dumps(rows, indent=2))
        return 0
    for r in rows:
        what = f"/{r['skill']}" if r["skill"] else (r["prompt"] or "").splitlines()[0][:50] if r["prompt"] else ""
        print(f"{r['name']:<24} {r['state']:<7} {r['every'] or '-':<16} {r['next_run'] or '-':<17} {r['account'] or '-':<10} {what}")
    return 0


def _warn_if_skill_is_unknown(skill: str):
    from pl import skills
    try:
        known = {item["name"] for item in skills.scan()}
    except SystemExit:
        return
    if skill not in known:
        print(f"pl loops: warning: skill {skill} is not in the library or any account", file=sys.stderr)


def _cmd_add(a) -> int:
    doc = _document()
    if a.name in doc.get("loops", {}):
        print(f"loop {a.name} exists, nothing changed")
        return 0
    entry = tomlkit.table()
    for key, value in (("skill", a.skill), ("prompt", a.prompt), ("every", a.every), ("hours", a.hours),
                       ("account", a.account), ("team", a.team)):
        if value:
            entry[key] = value
    entry["writes"] = bool(a.writes)
    if "every" in entry:
        try:
            schedule.parse_every(entry["every"])
        except ValueError as e:
            _fail(str(e))
    if a.skill:
        _warn_if_skill_is_unknown(a.skill)
    checked = tomlkit.parse(tomlkit.dumps(doc))
    checked.setdefault("loops", tomlkit.table())[a.name] = entry
    if errors := C.validate(checked):
        _fail("; ".join(errors))
    _confirm(f"Add loop {a.name}?", a.yes)
    if a.dry_run:
        print(f"would add loop {a.name}")
        return 0
    doc.setdefault("loops", tomlkit.table())[a.name] = entry
    _save(doc)
    print(f"added loop {a.name}")
    return 0


def _cmd_toggle(a, enabled: bool) -> int:
    doc = _document()
    if a.name not in doc.get("loops", {}):
        _fail(f"no loop {a.name} in config.toml")
    doc["loops"][a.name]["enabled"] = enabled
    _save(doc)
    print(f"loop {a.name} {'resumed' if enabled else 'paused'}")
    return 0


def _cmd_rm(a) -> int:
    doc = _document()
    if a.name not in doc.get("loops", {}):
        _fail(f"no loop {a.name} in config.toml")
    _confirm(f"Remove loop {a.name}?", a.yes)
    del doc["loops"][a.name]
    _save(doc)
    print(f"removed loop {a.name}")
    return 0


def cmd_loops(a):
    if a.loops_cmd == "run":
        if getattr(a, "dry_run", False):
            return dry_run(a)
        return run(a.name, wait=a.wait, as_json=a.json)
    return {"list": _cmd_list, "add": _cmd_add, "rm": _cmd_rm,
            "pause": lambda a: _cmd_toggle(a, False), "resume": lambda a: _cmd_toggle(a, True)}[a.loops_cmd](a)


def run(name, wait=False, as_json=False):
    svc = C.SERVICES.get(name)
    if not svc:
        raise SystemExit(f"pl loops run: no loop named {name}")
    if not svc.get("every"):
        raise SystemExit(f"pl loops run: {name} has no `every`, so it runs as one long-lived session and has nothing to run now")
    st = load_state()
    s = st.setdefault("services", {}).setdefault(name, {})
    before = s.get("last_run")
    s["run_now"] = True
    save_state(st)
    print("queued; the dispatcher starts it on its next pass")
    if not wait:
        return
    for _ in range(WAIT_POLLS):
        if (record := (load_state().get("services", {}).get(name) or {}).get("last_run")) != before:
            break
        time.sleep(2)
    else:
        raise SystemExit(f"pl loops run: the dispatcher has not picked it up; is it running? ({name} stays queued)")
    print(json.dumps(record) if as_json else f"exit {record.get('exit')}: {record.get('result')} (tokens {record.get('tokens')})")


DRY_RUN_TIMEOUT = 600


def dry_run(a):
    """Run a loop once in the foreground with the harness's read-only flags and PL_DRY_RUN=1, then print its result and
    the "would file" lines. Nothing is recorded as a run."""
    from pl import dispatch, harnesses
    svc = C.SERVICES.get(a.name)
    if svc is None:
        raise BadInput(f"pl loops run: no loop named {a.name}")
    account = svc["profile"] or next(iter(C.ACCOUNTS), None)
    if account is None:
        raise BadInput("pl loops run: no account to run it under")
    h = harnesses.read_only_headless(harnesses.account_harness(account))
    if h is None:
        raise BadInput(f"pl loops run: {harnesses.account_harness(account).name} has no read-only mode")
    argv, env = harnesses.headless_argv(h, account, dispatch.loop_prompt(svc))
    try:
        r = harnesses._run(argv, env={**env, **harnesses.loop_env(a.name, False, dry_run=True)}, stdin=harnesses.subprocess.DEVNULL,
                           timeout=DRY_RUN_TIMEOUT)
    except harnesses.subprocess.TimeoutExpired:
        raise BadInput(f"pl loops run: {a.name} timed out after {DRY_RUN_TIMEOUT}s")
    lines = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
    for ln in lines:
        if ln.startswith("would file:"):
            print(ln)
    print(f"result: {lines[-1] if lines else '(no output)'}")
    if r.returncode:
        raise SystemExit(r.returncode)
