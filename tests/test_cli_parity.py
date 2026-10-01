"""WP1 repro guard: the package's help output matches the legacy script's, command by command."""
import re
import subprocess
import sys
from pathlib import Path

import pytest

SNAPSHOT = Path.home() / "code" / "pl-legacy-snapshot" / "pl"
SRC = Path(__file__).resolve().parent.parent / "src"
SUBCOMMANDS = ["idea", "list", "review", "approve", "reject", "dispatch", "pull", "adopt", "done",
               "board", "card", "accounts", "watch", "pause", "resume", "intent"]


def _help(argv, env=None):
    r = subprocess.run(argv, capture_output=True, text=True, timeout=30, env={"PATH": "/usr/bin:/bin", "COLUMNS": "120", **(env or {})})
    return r.returncode, r.stdout, r.stderr


def _options(out):
    """WP14 rewrote the top-level description (it named company values): compare usage and options only."""
    return out.split("\n", 1)[0] + out[out.find("positional arguments:"):]


@pytest.mark.skipif(not SNAPSHOT.is_file(), reason="legacy snapshot not present")
@pytest.mark.parametrize("args", [["--help"], []] + [[s, "--help"] for s in SUBCOMMANDS])
def test_help_matches_legacy(args, tmp_path):
    legacy = _help([sys.executable, str(SNAPSHOT), *("profiles" if x == "accounts" else x for x in args)])
    prof = tmp_path / ".pl-t"
    prof.mkdir()
    (prof / "config.toml").write_text('[accounts.acme]\nconfig_dir = "~/a"\n[accounts.acme2]\nconfig_dir = "~/b"\n')
    new = _help([sys.executable, "-m", "pl", *args], {"HOME": str(tmp_path), "PL_CONFIG_DIR": str(prof)})
    want = _wp2_expected(args, legacy)
    if args in (["--help"], []):
        new, want = (new[0], _options(new[1]), new[2]), (want[0], _options(want[1]), want[2])
    assert new == want


def _wp2_expected(args, legacy):
    """WP2's only help changes: `idea`/`adopt` rename --profile to --account; top level gains --profile NAME."""
    code, out, err = legacy
    if args[:1] in (["idea"], ["adopt"]) or args in (["--help"], []):
        out = re.sub(r"(--profile )\{\w+,\w+\}", r"\1{acme,acme2}", out)   # the legacy script hard-coded its two account names
        out = re.sub(r"--profile \w+\|\w+", "--account acme|acme2", out).replace("[--profile {", "[--account {")
        out = out.replace("  --profile {", "  --account {")
    if args[:1] == ["accounts"]:
        out = out.replace("usage: pl profiles", "usage: pl accounts")
    if args in (["--help"], []):
        out = out.replace("{idea,list,review,approve,reject,dispatch,pull,adopt,done,board,card,profiles,",
                          "{idea,list,review,approve,reject,dispatch,pull,adopt,done,board,card,profiles,accounts,")
        out = out.replace(",adopt,done,board,", ",adopt,done,move,board,")   # WP28 adds pl move
        out = out.replace(",board,card,profiles,", ",board,card,retry,profiles,")   # WP42 adds pl retry
        out = out.replace(",pause,resume,intent", ",pause,resume,standup,intent")   # WP46 adds pl standup
        out = out.replace("resume,standup,intent}", "resume,standup,intent,usage}")   # pl usage (token spend)
        out = out.replace("intent,usage}", "intent,usage,alerts,move-agent}")   # pl alerts, pl move-agent (move a live agent in place)
        out = out.replace("alerts,move-agent}", "alerts,move-agent,assistant}")   # pl assistant (the Assistant tab's session)
        out = out.replace("move-agent,assistant}", "move-agent,assistant,skills}")   # pl skills (list, share, link)
        out = out.replace("assistant,skills}", "assistant,skills,update}")   # pl update (prints the update commands)
        out = out.replace("intent} ...\n", "intent}\n          ...\n", 1)  # the longer choice list no longer fits " ..." on its line
        out = out.replace("  pl profiles [--reset NAME|all]\n", WP3_DOC + "  pl accounts [--reset NAME|all]\n")
        out = out.replace("usage: pl [-h]\n", "usage: pl [-h] [--profile NAME] [--version]\n", 1)
        out = out.replace("show this help message and exit\n",
                          "show this help message and exit\n  --profile NAME        " + PROFILE_HELP + "\n"
                          "  --version             show program's version number and exit\n", 1)
    return code, out, err


WP3_DOC = """  pl profiles             every pl profile (~/.pl-NAME) and whether its dispatcher runs; warns when two share
                          a harness account, a tracker board or a tmux session
  pl profiles new NAME [--from-current]
                          create ~/.pl-NAME/config.toml (spec gate on) and print a shell alias for it
"""
PROFILE_HELP = "pl profile: settings, state and logs in ~/.pl-NAME (or $PL_CONFIG_DIR)"


def test_no_module_copies_config_names():
    offenders = [str(p) for p in SRC.rglob("*.py") if "from pl.config import" in p.read_text()]
    assert offenders == []


def test_no_config_rename_leaked_into_strings():
    import ast
    import re
    leak = re.compile(r"\bC\.[A-Z]")
    offenders = []
    for p in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(p.read_text())):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and leak.search(node.value):
                offenders.append(f"{p.name}:{node.lineno}: {node.value[:60]!r}")
    assert offenders == []
