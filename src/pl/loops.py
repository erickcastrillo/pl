"""pl loops: commands on the loops of a profile."""
import json
import time

from pl import config as C
from pl.util import load_state, save_state

WAIT_POLLS = 150


def cmd_loops(a):
    if a.loops_cmd == "run":
        return run(a.name, wait=a.wait, as_json=a.json)


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
