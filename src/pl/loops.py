"""pl loops: list|add|edit|pause|resume|rm manage [loops.<name>] in the profile's config.toml; run queues a run now."""
import json
import re
import sys
import time
from datetime import datetime

import tomlkit

from pl import config as C
from pl import schedule
from pl.util import BadInput, ask, load_state, save_state

WAIT_POLLS = 150

WORK_HOURS = "08:00-18:00 weekdays"
# Prompts name connector roles (email, payments), never a vendor.
TEMPLATES = {
    "auto-review": {"prompt": "Review the open pull requests that wait for review: read each diff and its checks, label each one ready, "
                              "rework or failed, and comment what you found. Never merge.",
                    "every": "30m", "hours": "08:00-20:00", "writes": True},
    "daily-standup": {"prompt": "Summarise what moved on the board since yesterday: cards done, in progress and blocked. "
                                "Write it as one short note for the owner.",
                      "every": "weekdays 09:00", "hours": WORK_HOURS, "writes": False},
    "inbox-triage": {"prompt": "Read the new mail through the email connector and group it as urgent, reply-needed or FYI. "
                               "List the first two groups with a one-line reason each. Do not send, move or delete anything.",
                     "every": "30m", "hours": WORK_HOURS, "writes": False, "needs": "email connector"},
    "overdue-invoices": {"prompt": "Through the payments connector, list the invoices past their due date, oldest first, with the customer, "
                                   "the amount and the days late. Propose a reminder for each one; do not send it.",
                         "every": "weekdays 09:00", "hours": WORK_HOURS, "writes": False, "needs": "payments connector"},
    "dependency-updates": {"prompt": "List the project's outdated dependencies and the ones with security advisories. Propose one update "
                                     "at a time with the risk of each; do not change any file.",
                           "every": "weekdays 07:00", "hours": "06:00-12:00 weekdays", "writes": False},
    "stale-cards": {"prompt": "Find the cards that have not moved for 7 days. List each with its column and age, and propose "
                              "nudge, drop or hold for it.",
                    "every": "weekdays 10:00", "hours": WORK_HOURS, "writes": False},
}

LOOP_PREFIX = re.compile(r"/loop (\d+)([mhd])(?:\s+|$)")


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


def add(name, skill=None, prompt=None, template=None, every=None, hours=None, account=None, team=None, writes=None,
        yes=True, dry_run=False):
    doc = _document()
    if name in doc.get("loops", {}):
        print(f"loop {name} exists, nothing changed")
        return
    base = {}
    if template:
        if template not in TEMPLATES:
            _fail(f"no template {template}; choose one of: {', '.join(TEMPLATES)}")
        base = dict(TEMPLATES[template])
        if skill:
            del base["prompt"]
    entry = tomlkit.table()
    for key, value in (("skill", skill), ("prompt", prompt or base.get("prompt")), ("every", every or base.get("every")),
                       ("hours", hours or base.get("hours")), ("account", account), ("team", team)):
        if value:
            entry[key] = value
    if "skill" not in entry and "prompt" not in entry:
        _fail("give --skill, --prompt or --template")
    entry["writes"] = base.get("writes", False) if writes is None else writes
    if "every" in entry:
        try:
            schedule.parse_every(entry["every"])
        except ValueError as e:
            _fail(str(e))
    if skill:
        _warn_if_skill_is_unknown(skill)
    checked = tomlkit.parse(tomlkit.dumps(doc))
    checked.setdefault("loops", tomlkit.table())[name] = entry
    if errors := C.validate(checked):
        _fail("; ".join(errors))
    _confirm(f"Add loop {name}?", yes)
    if dry_run:
        print(f"would add loop {name}")
        return
    doc.setdefault("loops", tomlkit.table())[name] = entry
    _save(doc)
    print(f"added loop {name}")


def _cmd_add(a) -> int:
    add(a.name, a.skill, a.prompt, a.template, a.every, a.hours, a.account, a.team, a.writes, a.yes, a.dry_run)
    return 0


def _converted(prompt: str | None) -> tuple[str, str]:
    """The `every` a leading `/loop Nm|Nh|Nd` stands for, and the prompt without that prefix."""
    m = LOOP_PREFIX.match(prompt or "")
    if not m:
        _fail("the prompt has no /loop prefix to convert")
    count, unit = int(m[1]), m[2]
    if unit == "d":
        _fail("a /loop day interval has no `every` of its own; pass --every, for example daily 09:00")
    every = f"{count}m" if unit == "m" else "hourly" if count == 1 else f"{count * 60}m"
    return every, prompt[m.end():]


def _table_for(doc, name: str):
    """The loop's [loops.<name>] table; a built-in loop, or a default one while [loops] is absent, is written out first."""
    if name not in C.SERVICES and name not in C.LOOPS_OFF:
        _fail(f"no loop {name}")
    written = lambda s: {"prompt": s["prompt"], **({"account": s["profile"]} if s.get("profile") else {})}   # noqa: E731
    if "loops" not in doc:   # [loops] replaces every default loop, so write them all
        doc["loops"] = {n: written(s) for n, s in C.SERVICES.items() if not s.get("builtin")}
    if name not in doc["loops"]:
        doc["loops"][name] = written(C.SERVICES[name])
    return doc["loops"][name]


def edit(name: str, skill=None, prompt=None, every=None, hours=None, account=None, writes=None, convert=False):
    doc = _document()
    entry = _table_for(doc, name)
    if skill and not prompt:
        entry.pop("prompt", None)
    if prompt and not skill:
        entry.pop("skill", None)
    for key, value in (("skill", skill), ("prompt", prompt), ("every", every), ("hours", hours), ("account", account)):
        if value is not None:
            entry[key] = value
    if writes is not None:
        entry["writes"] = writes
    if convert:
        converted, entry["prompt"] = _converted(entry.get("prompt"))
        entry["every"] = every or converted
    elif not any(v is not None for v in (skill, prompt, every, hours, account, writes)):
        _fail("nothing to change; give a field to set or --convert")
    if "every" in entry:
        try:
            schedule.parse_every(entry["every"])
        except ValueError as e:
            _fail(str(e))
    if skill:
        _warn_if_skill_is_unknown(skill)
    if errors := C.validate(tomlkit.parse(tomlkit.dumps(doc))):
        _fail("; ".join(errors))
    _save(doc)


def _cmd_edit(a) -> int:
    edit(a.name, a.skill, a.prompt, a.every, a.hours, a.account, a.writes, a.convert)
    print(f"edited loop {a.name}")
    return 0


def set_enabled(name: str, enabled: bool):
    doc = _document()
    if name not in doc.get("loops", {}):
        _fail(f"no loop {name} in config.toml")
    doc["loops"][name]["enabled"] = enabled
    _save(doc)


def _cmd_toggle(a, enabled: bool) -> int:
    set_enabled(a.name, enabled)
    print(f"loop {a.name} {'resumed' if enabled else 'paused'}")
    return 0


def remove(name: str):
    doc = _document()
    if name not in doc.get("loops", {}):
        _fail(f"no loop {name} in config.toml")
    del doc["loops"][name]
    _save(doc)


def _cmd_rm(a) -> int:
    if a.name not in _document().get("loops", {}):
        _fail(f"no loop {a.name} in config.toml")
    _confirm(f"Remove loop {a.name}?", a.yes)
    remove(a.name)
    print(f"removed loop {a.name}")
    return 0


def cmd_loops(a):
    if a.loops_cmd == "run":
        if getattr(a, "dry_run", False):
            return dry_run(a)
        return run(a.name, wait=a.wait, as_json=a.json)
    return {"list": _cmd_list, "add": _cmd_add, "edit": _cmd_edit, "rm": _cmd_rm,
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
