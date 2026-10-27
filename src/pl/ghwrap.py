"""The `gh` that agents launched by pl find first on their PATH: `python -m pl.ghwrap ARGS...`.

It runs the real gh with the same argv and returns its exit code, then counts what a GraphQL call cost in the shared
GitHub ledger (ghquota) under the caller "<profile> agent <stage> <card>". The cost is the drop in remaining points
against one free rateLimit reading taken after the call. It never reads, prints or stores a token: the ledger account
comes from PL_GH_LEDGER, set by pl at launch.
"""
import os
import shutil
import subprocess
import sys
import time

from pl import config as C
from pl import ghquota, ghrest
from pl.trackers import github

ESTIMATE = 1   # points for a GraphQL call when no reading can say what it cost
WAITS = {("pr", "view"), ("pr", "list"), ("pr", "status"), ("pr", "checks"), ("api", "graphql")}
NO_CALL = {"", "--version", "version", "help", "--help", "auth", "config", "alias", "completion", "extension"}


def _run(argv, env):
    """The one subprocess call: the real gh, nothing captured, no timeout (agents run `gh pr checks --watch`)."""
    return subprocess.run(argv, env=env).returncode


_sleep = time.sleep


def _waits(args):
    return tuple(args[:2]) in WAITS or args[:1] == ["project"]


def _held(acct):
    wait = ghquota.check("read", acct)
    if wait and ghquota.stale(acct) and (budget := github._graphql_budget()) and budget[1]:
        ghquota.refresh(*budget, acct=acct)
        wait = ghquota.check("read", acct)
    return wait


def _hold(acct, args):
    """Wait for the reset while the ledger is below the write reserve, saying why on stderr once a minute."""
    said = None
    while (wait := _held(acct)) and (now := ghquota._clock()) < wait[0]:
        if said is None or now - said >= 60:
            at = time.strftime("%H:%M", time.localtime(wait[0]))
            print(f"pl: gh {' '.join(args[:2])} waits for GitHub's reset at {at}: {wait[1]}", file=sys.stderr)
            said = now
        _sleep(min(60, wait[0] - now))


def kind(args):
    """"none" for gh commands that call no API, "rest" for gh api on a REST endpoint, else "graphql"."""
    if not args or args[0] in NO_CALL:
        return "none"
    if args[0] == "api":
        endpoint = next((a for a in args[1:] if not a.startswith("-")), "")
        return "graphql" if endpoint == "graphql" else "rest"
    return "graphql"


def _caller():
    stage = os.environ.get("PL_STAGE") or os.environ.get("PL_LOOP") or "-"
    return f"{C.PROFILE_NAME or '-'} agent {stage} {os.environ.get('PL_CARD') or '-'}"


def _record(args):
    acct = os.environ.get("PL_GH_LEDGER")
    if not acct:
        return
    budget = github._graphql_budget()
    led = ghquota.ledger(acct)
    sub, caller = " ".join(args[:2]), _caller()
    if budget and budget[1] and led.get("reset") == budget[1] and isinstance(led.get("remaining"), int):
        ghquota.record_agent(acct, caller, sub, max(0, led["remaining"] - budget[0]), *budget)
    elif isinstance(led.get("remaining"), int) and (led.get("reset") or 0) > github._clock():
        ghquota.record_agent(acct, caller, sub, ESTIMATE, max(0, led["remaining"] - ESTIMATE), led["reset"])


def _answer_through_rest(real, args):
    """Below the write reserve, print the REST answer to a pr view/list and count it as REST; False when it cannot."""
    try:
        acct = os.environ["PL_GH_LEDGER"]
        led = ghquota.ledger(acct)
        if not ghrest.below_reserve(led, github._clock()):
            return False
        out = ghrest.answer(real, args, dict(os.environ))
        if out is None:
            return False
    except Exception:
        return False
    print(out)
    try:
        ghquota.record_agent(acct, _caller(), " ".join(args[:2]), 0, led["remaining"], led["reset"], rest=True)
    except Exception:
        pass   # counting never changes what the agent's gh call returned
    return True


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    shim = os.environ.get("PL_GH_SHIM_DIR")
    counting = True
    try:
        C.load()
        shim = shim or str(C.STATE_DIR / "bin")
    except (Exception, SystemExit):
        counting = False   # a profile being edited or removed must not stop the agent's gh
    os.environ["PATH"] = os.pathsep.join(p for p in os.environ.get("PATH", "").split(os.pathsep) if p != shim)
    real = shutil.which("gh")
    if not real:
        print("pl: gh is not installed: https://cli.github.com", file=sys.stderr)
        return 127
    # REST costs no GraphQL points, so it is tried before the hold, which waits in exactly this state
    if counting and os.environ.get("PL_GH_LEDGER") and args[:2] in (["pr", "view"], ["pr", "list"]):
        if _answer_through_rest(real, args):
            return 0
    if counting and _waits(args) and (acct := os.environ.get("PL_GH_LEDGER")):
        try:
            _hold(acct, args)
        except KeyboardInterrupt:
            return 130
        except Exception:
            pass   # a broken ledger never blocks the agent's gh
    code = _run([real, *args], dict(os.environ))
    if counting and kind(args) == "graphql":
        try:
            _record(args)
        except Exception:
            pass   # counting never changes what the agent's gh call returned
    return code


if __name__ == "__main__":
    sys.exit(main())
