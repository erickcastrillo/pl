"""pl models set-stage: the model a stage uses on one harness, normal or for a small change, in config.toml."""
import tomlkit

from pl import config as C
from pl import harnesses


def key_path(stage, harness, small):
    if small:
        return ("stages", stage, "small", "models", harness)
    return ("stages", stage, "models", harness)


def check(stage, harness) -> str | None:
    takers = ", ".join(harnesses.MODEL_FLAGS)
    if stage not in C.PROMPTS:
        return f"pl: unknown stage {stage!r}; stages: {', '.join(C.PROMPTS)}"
    if harness in harnesses.BUILTINS and harness not in harnesses.MODEL_FLAGS:
        return f"pl: harness {harness!r} takes no model; models take {takers}"
    if harness not in harnesses.MODEL_FLAGS:
        return f"pl: unknown harness {harness!r}; harnesses that take a model: {takers}"
    return None


def set_stage_model(doc, stage, harness, model, small) -> bool:
    """Set the key, or with an empty model remove it and the tables it leaves empty (never [stages.<stage>]).
    Edits the tomlkit doc in place; True when something changed."""
    path = key_path(stage, harness, small)
    tables = [doc]
    for k in path[:-1]:
        t = tables[-1]
        if k not in t:
            if not model:
                return False
            t[k] = tomlkit.table()
        tables.append(t[k])
    if model:
        tables[-1][harness] = model
        return True
    if harness not in tables[-1]:
        return False
    del tables[-1][harness]
    for i in range(len(path) - 1, 2, -1):
        if len(tables[i]):
            break
        del tables[i - 1][path[i - 1]]
    return True


def cmd_models(a):
    from pl import cli
    if C.path() is None:
        raise SystemExit(cli.NO_PROFILE)
    err = check(a.stage, a.harness)
    if err:
        raise SystemExit(err)
    doc = tomlkit.parse(C.path().read_text())
    shown = ".".join(key_path(a.stage, a.harness, a.small))
    if not set_stage_model(doc, a.stage, a.harness, a.model, a.small):
        print(f"{shown} is not set; nothing to remove")
        return
    C.save(doc)
    print(f'{shown} = "{a.model}"' if a.model else f"removed {shown}")
