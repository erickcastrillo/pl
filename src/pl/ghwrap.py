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

from pl import config as C
from pl import ghquota
from pl.trackers import github

ESTIMATE = 1   # points for a GraphQL call when no reading can say what it cost
NO_CALL = {"", "--version", "version", "help", "--help", "auth", "config", "alias", "completion", "extension"}


def _run(argv, env):
    """The one subprocess call: the real gh, nothing captured, no timeout (agents run `gh pr checks --watch`)."""
    return subprocess.run(argv, env=env).returncode


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


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    C.load()
    shim = str(C.STATE_DIR / "bin")
    os.environ["PATH"] = os.pathsep.join(p for p in os.environ.get("PATH", "").split(os.pathsep) if p != shim)
    real = shutil.which("gh")
    if not real:
        print("pl: gh is not installed: https://cli.github.com", file=sys.stderr)
        return 127
    code = _run([real, *args], dict(os.environ))
    if kind(args) == "graphql":
        try:
            _record(args)
        except Exception:
            pass   # counting never changes what the agent's gh call returned
    return code


if __name__ == "__main__":
    sys.exit(main())
