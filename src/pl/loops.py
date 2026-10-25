"""`pl loops`: the commands that run a loop by hand."""
from pl import config as C
from pl import dispatch, harnesses
from pl.util import BadInput


def cmd_loops(a):
    if a.loops_cmd == "run":
        return dry_run(a)


DRY_RUN_TIMEOUT = 600


def dry_run(a):
    """Run a loop once in the foreground with the harness's read-only flags and PL_DRY_RUN=1, then print its result and
    the "would file" lines. Nothing is recorded as a run."""
    if not a.dry_run:
        raise BadInput("pl loops run: only --dry-run is available so far")
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
