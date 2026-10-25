"""The one way a command prints --json: a single masked document. SHAPES names each shape in docs/cli-json.md."""
import argparse
import json

from pl.util import mask

SHAPES = {"list": "board", "card": "card", "section": "section", "alerts": "alerts",
          "usage": "usage", "standup": "standup", "accounts": "accounts", "profiles": "profiles", "intent": "intent",
          "manager-status": "manager-status", "local-model": "local-model", "whatsnew": "whatsnew",
          "skills-list": "skills-list", "update-check": "update-check"}


def emit(data):
    print(mask(json.dumps(data, ensure_ascii=False, default=str)))


def _flags(parser):
    out = []
    for act in parser._actions:
        if isinstance(act, (argparse._HelpAction, argparse._SubParsersAction, argparse._VersionAction)):
            continue
        kind = "flag" if act.nargs == 0 else getattr(act.type, "__name__", "str")
        out.append({"name": act.option_strings[0] if act.option_strings else act.dest, "type": kind,
                    "choices": list(act.choices) if act.choices else None, "required": bool(act.required), "help": act.help})
    return out


def commands(parser, pre=(), path=()):
    """Every command of the parser with its flags, its JSON shape name and its sub-actions; `pre` names the commands parsed before it."""
    out = []
    for act in parser._actions:
        if isinstance(act, argparse._SubParsersAction):
            for name, sp in act.choices.items():
                here = (*path, name)
                out.append({"name": " ".join(here), "shape": SHAPES.get("-".join(here)), "flags": _flags(sp),
                            "commands": commands(sp, path=here)})
    return out + [{"name": n, "shape": SHAPES.get(n), "flags": [], "commands": [], "own_parser": True} for n in pre]
