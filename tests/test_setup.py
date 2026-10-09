"""WP16: `pl setup` writes a complete, validated profile from answers (flags or prompts)."""
import datetime
import json
import os
import re
import stat
import subprocess
import sys
import tomllib

import pytest

from pl import cli, setup
from pl import config as C

FAKE_GH = r"""#!/bin/sh
echo "GH_CONFIG_DIR=${GH_CONFIG_DIR:-} $*" >> "$GH_LOG"
case "$1 $2" in
  "auth status")
    if [ -n "$GH_UNSIGNED" ]; then echo "You are not logged into any GitHub hosts." >&2; exit 1; fi
    if [ -n "$GH_CONFIG_DIR" ] && [ ! -e "$GH_CONFIG_DIR/signed-in" ]; then
      echo "You are not logged into any GitHub hosts." >&2; exit 1
    fi
    echo "github.com"
    echo "  Logged in to github.com account octo (keyring)"
    echo "  - Token: gho_PLANTEDTOKEN"
    echo "  Logged in to github.com account octo-work (keyring)"
    exit 0 ;;
  "project create") echo '{"number": 7, "url": "https://github.com/orgs/acme/projects/7"}' ;;
  "project view") echo '{"number": 3, "url": "https://github.com/orgs/acme/projects/3"}' ;;
  "project field-list") [ -n "$GH_UNSIGNED" ] && { echo "not logged in" >&2; exit 4; }; exec "$PY" "$FAKE_GQL" "$@" ;;
  "project field-create") exec "$PY" "$FAKE_GQL" "$@" ;;
  "api graphql") [ -n "$GH_UNSIGNED" ] && exit 4; exec "$PY" "$FAKE_GQL" "$@" ;;
  "label list") [ -n "$GH_UNSIGNED" ] && exit 4; cat "$LABELS_JSON" 2>/dev/null || echo '[]' ;;
  "label create") exit 0 ;;
  *) echo "unexpected gh $*" >&2; exit 9 ;;
esac
"""

# The project behind the fake gh: field-list, field-create and `api graphql` read and change project.json.
# GraphQL arguments are parsed from the query text, the way GitHub would read them.
FAKE_GQL = r"""
import json, os, re, sys
P = os.environ["PROJECT_JSON"]
S = json.load(open(P))
args = sys.argv[1:]


def done(x=None):
    json.dump(S, open(P, "w"))
    if x is not None:
        print(json.dumps(x))
    sys.exit(0)


def fail(msg):
    print("gh: " + msg, file=sys.stderr)
    sys.exit(1)


def to_json(lit):
    out, i = [], 0
    while i < len(lit):
        c = lit[i]
        if c == '"':
            j = i + 1
            while lit[j] != '"':
                j += 2 if lit[j] == "\\" else 1
            out.append(lit[i:j + 1])
            i = j + 1
        elif c.isalpha() or c == "_":
            w = re.match(r"\w+", lit[i:]).group()
            i += len(w)
            key = lit[i:].lstrip().startswith(":")
            out.append(w if (w in ("true", "false", "null") and not key) else '"' + w + '"')
        else:
            out.append(c)
            i += 1
    return json.loads("".join(out))


KIND = {"SINGLE_SELECT": "ProjectV2SingleSelectField", "ITERATION": "ProjectV2IterationField"}
if args[:2] == ["project", "field-list"]:
    done({"fields": [{"id": f["id"], "name": f["name"], "type": KIND.get(f["dataType"], "ProjectV2Field"),
                      **({"options": [{"id": o["id"], "name": o["name"]} for o in f["options"]]} if "options" in f else {})}
                     for f in S["fields"]], "totalCount": len(S["fields"])})
if args[:2] == ["project", "field-create"]:
    name = args[args.index("--name") + 1]
    opts = args[args.index("--single-select-options") + 1].split(",")
    S["fields"].append({"id": "PVTSSF_stage", "name": name, "dataType": "SINGLE_SELECT",
                        "options": [{"id": f"c{i}", "name": n, "color": "GRAY", "description": ""} for i, n in enumerate(opts)]})
    done()
q = next(a[len("query="):] for a in args if a.startswith("query="))
if q.startswith("query"):
    m = re.match(r"query\{(user|organization)\(login:", q)
    if m.group(1) != os.environ.get("GQL_OWNER_KIND", "organization"):
        fail("GraphQL: Could not resolve to a User with the login of 'acme'. (user)")
    done({"data": {m.group(1): {"projectV2": {"id": S["id"], "views": {"nodes": S["views"]},
                                                "fields": {"nodes": S["fields"]}}}}})
if os.environ.get("GQL_FAIL"):
    fail("GraphQL: Resource not accessible by integration (" + q[:40] + ")")
m = re.fullmatch(r"mutation\{(\w+)\(input:(\{.*\})\)\{clientMutationId\}\}", q, re.S)
name, inp = m.group(1), to_json(m.group(2))
if name == "updateProjectV2View":
    vid = inp.pop("viewId")
    v = next(v for v in S["views"] if v["id"] == vid)
    v.update(inp)
elif name == "createProjectV2View":
    assert inp.pop("projectId") == S["id"] and set(inp) == {"name", "layout"}
    n = max(v["number"] for v in S["views"]) + 1
    S["views"].append({"id": f"PVTV_{n}", "number": n, "filter": None, **inp})
elif name == "updateProjectV2Field":
    f = next(f for f in S["fields"] if f["id"] == inp["fieldId"])
    have = {o["id"] for o in f["options"]}
    new = []
    for i, o in enumerate(inp["singleSelectOptions"]):
        assert o.get("id", "new") == "new" or o["id"] in have, o
        new.append({**o, "id": o["id"] if "id" in o and not os.environ.get("GQL_DROP_IDS") else f"n{i}"})
    f["options"] = new
elif name == "createProjectV2Field":
    assert inp.pop("projectId") == S["id"]
    S["fields"].append({"id": "PVTIF_" + inp["name"], "name": inp["name"], "dataType": inp["dataType"]})
else:
    fail("unknown mutation " + name)
done({"data": {name: {"clientMutationId": None}}})
"""


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("PL_CONFIG_DIR", "PL_TMUX_SESSION", "GH_CONFIG_DIR"):
        monkeypatch.delenv(var, raising=False)
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    _exe(bin_ / "gh", FAKE_GH)
    _exe(bin_ / "claude", "#!/bin/sh\nexit 0\n")
    _exe(bin_ / "uv", '#!/bin/sh\necho "uv $*" >> "$GH_LOG"\n')
    monkeypatch.setenv("PATH", f"{bin_}:/usr/bin:/bin")
    monkeypatch.setenv("GH_LOG", str(tmp_path / "gh.log"))
    monkeypatch.setenv("PROJECT_JSON", str(tmp_path / "project.json"))
    monkeypatch.setenv("PY", sys.executable)
    (tmp_path / "fake_gql.py").write_text(FAKE_GQL)
    monkeypatch.setenv("FAKE_GQL", str(tmp_path / "fake_gql.py"))
    for var in ("GQL_FAIL", "GQL_DROP_IDS", "GQL_OWNER_KIND", "GH_UNSIGNED"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LABELS_JSON", str(tmp_path / "labels.json"))   # absent: the repo has no labels
    _fields(tmp_path)                            # only GitHub's built-in Status field
    (tmp_path / ".claude").mkdir()
    (tmp_path / "work").mkdir()
    monkeypatch.chdir(tmp_path)                  # not a git repo
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    # a clone whose pl is installed as a tool and found by the login shell: the PATH check is quiet by default
    tools = tmp_path / ".local" / "bin"
    tools.mkdir(parents=True)
    _exe(tools / "pl", "#!/bin/sh\n")
    _exe(bin_ / "uv", '#!/bin/sh\necho "uv $*" >> "$GH_LOG"\ncase "$1 $2" in "tool list") echo "pl-funnel v0.1.0";;\n'
                      f'  "tool dir") echo "{tools}";; esac\n')
    _exe(tmp_path / "loginsh", f'#!/bin/sh\necho "{tools / "pl"}"\n')
    monkeypatch.setenv("SHELL", str(tmp_path / "loginsh"))
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "clone" / ".venv"))
    C.load()
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


def _exe(p, text):
    p.write_text(text)
    p.chmod(0o755)


STATUS = {"id": "F1", "name": "Status", "dataType": "SINGLE_SELECT",
          "options": [{"id": "s1", "name": "Todo", "color": "GREEN", "description": ""},
                      {"id": "s2", "name": "Done", "color": "PURPLE", "description": ""}]}
VIEW_1 = {"id": "PVTV_1", "number": 1, "name": "View 1", "layout": "TABLE_LAYOUT", "filter": None}


def _fields(home, stage_options=None, views=(VIEW_1,)):
    fields = [STATUS]
    if stage_options is not None:
        fields.append({"id": "PVTSSF_stage", "name": "pl stage", "dataType": "SINGLE_SELECT",
                       "options": [{"id": f"o{i}", "name": n, "color": "BLUE", "description": f"about {n}"}
                                   for i, n in enumerate(stage_options)]})
    (home / "project.json").write_text(json.dumps({"id": "PVT_1", "fields": fields, "views": list(views)}))


def _project(home):
    return json.loads((home / "project.json").read_text())


def _writes(home):
    """Every gh call that could change a project; none may ever name the built-in Status field."""
    w = [x for x in _gh_log(home).splitlines() if (x.split(" ", 1)[1].startswith(("project field-", "project item-edit"))
         and "field-list" not in x) or "query=mutation" in x]
    assert not any("Status" in x or "F1" in x or "field-delete" in x or "delete" in x.lower() for x in w)
    return w


def _field_writes(home):
    return [x for x in _writes(home) if "View" not in x]


def _mutations(home):
    """(name, input) of every GraphQL write, parsed the way the fake GitHub parses them."""
    ns = {"re": re, "json": json}
    exec(FAKE_GQL[FAKE_GQL.index("def to_json"):FAKE_GQL.index("KIND = ")], ns)     # the fake's own parser
    out = []
    for x in _writes(home):
        if "query=mutation" in x:
            m = re.fullmatch(r"mutation\{(\w+)\(input:(\{.*\})\)\{clientMutationId\}\}", x.split("query=", 1)[1])
            out.append((m.group(1), ns["to_json"](m.group(2))))
    return out


def _pl(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["pl", "setup", *args])
    return cli.main()


def _gh_log(home):
    p = home / "gh.log"
    return p.read_text() if p.exists() else ""


def _cfg(home, slug):
    return tomllib.loads((home / f".pl-{slug}" / "config.toml").read_text())


BASE = ["--yes", "--name", "Work – Acme", "--harness", "claude", "--work-dir", "~/work"]


def test_github_project_existing_writes_a_complete_valid_profile(fake_home, monkeypatch, capsys):
    gh = fake_home / "ghw"
    gh.mkdir()
    (gh / "signed-in").write_text("")
    _pl(monkeypatch, *BASE, "--tracker", "github-project", "--owner", "acme", "--project-number", "3",
        "--repo", "acme/app", "--gh-config-dir", str(gh))
    out = capsys.readouterr().out
    d = fake_home / ".pl-work-acme"
    t = _cfg(fake_home, "work-acme")
    assert stat.S_IMODE((d / "config.toml").stat().st_mode) == 0o600
    assert t["tracker"] == {"type": "github-project", "owner": "acme", "number": 3, "repo": "acme/app",
                            "status_field": "pl stage"}
    assert t["code_host"]["gh_config_dir"] == "~/ghw" and t["code_host"]["owner"] == "acme"
    assert t["accounts"] == {"claude": {"harness": "claude", "config_dir": "~/.claude"}}
    assert {s: v["account"] for s, v in t["stages"].items()} == {s: "claude" for s in ("spec", "design", "plan", "run")}
    assert t["paths"]["work_dir"] == "~/work" and "plans_dir" not in t["paths"]
    assert t["gates"]["spec"] is True and t["name"] == "Work – Acme"
    assert C.validate(t) == []
    C.load("work-acme")
    assert C.GH_CONFIG_DIR == gh and C.WORK_DIR == fake_home / "work"
    assert "alias pl-work-acme='PL_CONFIG_DIR=~/.pl-work-acme pl'" in out
    assert "pl --profile work-acme list" in out
    assert "gho_PLANTEDTOKEN" not in out


GITHUB_EXISTING = [*BASE, "--tracker", "github-project", "--owner", "acme", "--project-number", "3", "--repo", "acme/app"]


def test_stage_field_missing_is_created_with_every_column(fake_home, monkeypatch, capsys):
    _pl(monkeypatch, *GITHUB_EXISTING)
    out = capsys.readouterr().out
    assert _field_writes(fake_home) == ["GH_CONFIG_DIR= project field-create 3 --owner acme --name pl stage --data-type SINGLE_SELECT "
                                  "--single-select-options " + ",".join(C.COLUMNS)]
    assert "Column by" in out and "https://github.com/orgs/acme/projects/3" in out
    assert "ACTION NEEDED (optional): " in out and "ACTION NEEDED: " not in out


def test_stage_field_complete_is_left_alone(fake_home, monkeypatch, capsys):
    _fields(fake_home, ["Extra", *C.COLUMNS])
    _pl(monkeypatch, *GITHUB_EXISTING)
    assert _field_writes(fake_home) == [] and "Column by" in capsys.readouterr().out


def test_stage_field_missing_options_without_consent_are_printed_never_written(fake_home, monkeypatch, capsys):
    _fields(fake_home, ["Inbox", "Mine"])
    with pytest.raises(SystemExit) as e:
        _pl(monkeypatch, *GITHUB_EXISTING, "--no-add-missing-stages")
    out = capsys.readouterr().out
    assert e.value.code == 3 and _field_writes(fake_home) == []
    assert "ACTION NEEDED: " in out and "Spec ready, Plan for review" in out and "Inbox," not in out
    assert _cfg(fake_home, "work-acme")["tracker"]["status_field"] == "pl stage"


@pytest.mark.parametrize("consent", [[], ["--add-missing-stages"]])
def test_stage_field_missing_options_are_added_keeping_every_old_option(fake_home, monkeypatch, capsys, consent):
    _fields(fake_home, ["Mine", "Inbox"])
    old = _project(fake_home)["fields"][1]["options"]
    _pl(monkeypatch, *GITHUB_EXISTING, *consent)
    out = capsys.readouterr().out
    upd = [i for n, i in _mutations(fake_home) if n == "updateProjectV2Field"]
    assert len(upd) == 1 and upd[0]["fieldId"] == "PVTSSF_stage"
    sent = upd[0]["singleSelectOptions"]
    assert sent[:2] == old                                            # every old option, id, color, description as it was
    assert sent[2:] == [{"name": c, "color": "GRAY", "description": ""} for c in C.COLUMNS if c != "Inbox"]
    after = _project(fake_home)["fields"][1]["options"]
    assert after[:2] == old and [o["name"] for o in after[2:]] == [c for c in C.COLUMNS if c != "Inbox"]
    assert _project(fake_home)["fields"][0] == STATUS and "ACTION NEEDED: " not in out


def test_stage_option_ids_that_change_are_reported_loudly(fake_home, monkeypatch, capsys):
    _fields(fake_home, ["Mine", "Inbox"])
    monkeypatch.setenv("GQL_DROP_IDS", "1")
    with pytest.raises(SystemExit) as e:
        _pl(monkeypatch, *GITHUB_EXISTING)
    out = capsys.readouterr().out
    assert e.value.code == 3 and "ACTION NEEDED: " in out and "Mine, Inbox" in out and "new ids" in out


# ---------- WP19: views and sprint ----------

NEEDS_FILTER = '"pl stage":"Spec ready","Plan for review"'


def _views(home):
    return {v["name"]: v for v in _project(home)["views"]}


def test_first_run_renames_view_1_to_a_pipeline_board_and_adds_needs_you(fake_home, monkeypatch, capsys):
    _pl(monkeypatch, *GITHUB_EXISTING)
    out = capsys.readouterr().out
    views = [(n, i) for n, i in _mutations(fake_home) if "View" in n]
    assert views == [("updateProjectV2View", {"viewId": "PVTV_1", "name": "Pipeline", "layout": "BOARD_LAYOUT"}),
                     ("createProjectV2View", {"projectId": "PVT_1", "name": "Needs you", "layout": "TABLE_LAYOUT"}),
                     ("updateProjectV2View", {"viewId": "PVTV_2", "filter": NEEDS_FILTER})]
    v = _views(fake_home)
    assert set(v) == {"Pipeline", "Needs you"} and v["Pipeline"]["id"] == "PVTV_1"
    assert v["Needs you"]["filter"] == NEEDS_FILTER and v["Needs you"]["layout"] == "TABLE_LAYOUT"
    assert "ACTION NEEDED (optional): " in out and "Pipeline" in out and "Column by" in out
    assert "ACTION NEEDED: " not in out


@pytest.mark.parametrize("have", [[VIEW_1, {**VIEW_1, "id": "PVTV_2", "number": 2, "name": "Needs you", "filter": "x"}],
                                  [{**VIEW_1, "name": "Backlog"}]])
def test_pipeline_is_created_when_view_1_is_not_the_only_default_view(fake_home, monkeypatch, capsys, have):
    _fields(fake_home, views=have)
    _pl(monkeypatch, *GITHUB_EXISTING)
    m = [(n, i) for n, i in _mutations(fake_home) if "View" in n]
    assert m[0] == ("createProjectV2View", {"projectId": "PVT_1", "name": "Pipeline", "layout": "BOARD_LAYOUT"})
    assert not any(i.get("viewId") in [v["id"] for v in have] for _, i in m)       # the old views are untouched
    assert [v for v in _project(fake_home)["views"] if v["id"] in [h["id"] for h in have]] == have
    names = [v["name"] for v in _project(fake_home)["views"]]
    assert sorted(names) == sorted({*names, "Pipeline", "Needs you"})     # both present, no duplicates


def test_views_with_those_names_are_left_alone(fake_home, monkeypatch, capsys):
    have = [{**VIEW_1, "name": "Pipeline"}, {**VIEW_1, "id": "PVTV_2", "number": 2, "name": "Needs you", "filter": "x"}]
    _fields(fake_home, list(C.COLUMNS), views=have)
    _pl(monkeypatch, *GITHUB_EXISTING)
    assert _writes(fake_home) == [] and _project(fake_home)["views"] == have


def test_no_views_sets_up_none(fake_home, monkeypatch, capsys):
    _pl(monkeypatch, *GITHUB_EXISTING, "--no-views")
    out = capsys.readouterr().out
    assert [n for n, _ in _mutations(fake_home) if "View" in n] == [] and _views(fake_home) == {"View 1": VIEW_1}
    assert "switch the view to Board" in out


def test_views_declined_at_the_prompt(fake_home, monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    prompts = []
    monkeypatch.setattr("builtins.input", lambda p="": (prompts.append(p), "n" if "views" in p else "")[1])
    _pl(monkeypatch, *GITHUB_EXISTING[1:])
    assert "Set up the Project's views (Pipeline board, Needs you)? [Y/n]: " in prompts
    assert "Add a 2-week Sprint field and a 'This sprint' board? [y/N]: " in prompts
    assert [n for n, _ in _mutations(fake_home) if n != "updateProjectV2Field"] == []


def test_sprint_is_off_by_default(fake_home, monkeypatch, capsys):
    _pl(monkeypatch, *GITHUB_EXISTING)
    assert "This sprint" not in _views(fake_home) and "Sprint" not in [f["name"] for f in _project(fake_home)["fields"]]


def test_sprint_adds_an_iteration_field_and_a_this_sprint_board(fake_home, monkeypatch, capsys):
    _pl(monkeypatch, *GITHUB_EXISTING, "--sprint", "--sprint-weeks", "3")
    today = datetime.date.today().isoformat()
    m = _mutations(fake_home)
    assert ("createProjectV2Field", {"projectId": "PVT_1", "dataType": "ITERATION", "name": "Sprint",
                                     "iterationConfiguration": {"startDate": today, "duration": 21, "iterations": [
                                         {"title": "Sprint 1", "startDate": today, "duration": 21}]}}) in m
    assert ("createProjectV2View", {"projectId": "PVT_1", "name": "This sprint", "layout": "BOARD_LAYOUT"}) in m
    v = _views(fake_home)["This sprint"]
    assert ("updateProjectV2View", {"viewId": v["id"], "filter": "sprint:@current"}) in m and v["filter"] == "sprint:@current"


def test_a_rerun_changes_nothing(fake_home, monkeypatch, capsys):
    _fields(fake_home, ["Mine"])
    _pl(monkeypatch, *GITHUB_EXISTING, "--sprint")
    first = _gh_log(fake_home)
    assert len(_mutations(fake_home)) == 7                  # options, Pipeline, Needs you + filter, Sprint, This sprint + filter
    state = _project(fake_home)
    (fake_home / "gh.log").write_text("")
    _pl(monkeypatch, "--yes", "--slug", "work-acme", "--sprint")
    assert _writes(fake_home) == [] and _project(fake_home) == state
    (fake_home / "gh.first.log").write_text(first)


def test_owner_is_read_as_a_user_first_then_as_an_organisation(fake_home, monkeypatch, capsys):
    monkeypatch.setenv("GQL_OWNER_KIND", "user")
    _pl(monkeypatch, *GITHUB_EXISTING)
    reads = [x for x in _gh_log(fake_home).splitlines() if "query=query" in x]
    assert reads and all("query=query{user(login:" in x for x in reads) and "Needs you" in _views(fake_home)


def test_graphql_errors_are_an_action_needed_and_the_profile_is_still_written(fake_home, monkeypatch, capsys):
    monkeypatch.setenv("GQL_FAIL", "1")
    with pytest.raises(SystemExit) as e:
        _pl(monkeypatch, *GITHUB_EXISTING, "--sprint")
    out = capsys.readouterr().out
    assert e.value.code == 3 and "ACTION NEEDED: " in out and "Resource not accessible" in out
    assert _cfg(fake_home, "work-acme")["tracker"]["number"] == 3


def test_create_project_records_the_repo(fake_home, monkeypatch, capsys):
    _pl(monkeypatch, *BASE, "--tracker", "github-project", "--owner", "acme", "--create-project", "--title", "Acme work",
        "--repo", "acme/app")
    t = _cfg(fake_home, "work-acme")["tracker"]
    assert t == {"type": "github-project", "owner": "acme", "number": 7, "status_field": "pl stage", "repo": "acme/app"}
    assert "project create --owner acme --title Acme work" in _gh_log(fake_home)
    out = capsys.readouterr().out
    assert "https://github.com/orgs/acme/projects/7" in out and "Column by" in out
    assert "field-list" not in _gh_log(fake_home) and len(_field_writes(fake_home)) == 1
    assert "Needs you" in _views(fake_home) and "Pipeline" in _views(fake_home)


def test_github_issues_with_the_default_gh_sign_in_lists_logins_only(fake_home, monkeypatch, capsys):
    _pl(monkeypatch, *BASE, "--tracker", "github-issues", "--repo", "acme/app")
    t = _cfg(fake_home, "work-acme")
    out = capsys.readouterr().out
    assert t["tracker"] == {"type": "github-issues", "repo": "acme/app"}
    assert "gh_config_dir" not in t["code_host"] and t["code_host"]["owner"] == "acme"
    assert "octo" in out and "octo-work" in out and "gho_PLANTEDTOKEN" not in out
    assert C.validate(t) == []


def test_mcp_lists_server_names_and_never_prints_a_value(fake_home, monkeypatch, capsys):
    (fake_home / "work" / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "board": {"command": "board-mcp", "env": {"BOARD_TOKEN": "sekrit-planted-123"}},
        "notes": {"url": "https://x.test/mcp", "headers": {"Authorization": "Bearer sekrit-planted-456"}}}}))
    with pytest.raises(SystemExit) as e:
        _pl(monkeypatch, *BASE, "--tracker", "mcp", "--server", "board")
    out = capsys.readouterr().out
    assert e.value.code == 3 and "ACTION NEEDED: add a [tracker.tools] table" in out
    t = _cfg(fake_home, "work-acme")
    assert t["tracker"] == {"type": "mcp", "mcp_config": str(fake_home / "work" / ".mcp.json"), "server": "board"}
    assert "board" in out and "notes" in out and "docs/mcp-trackers.md in the pl repository" in out and "[tracker.tools]" in out
    assert "PR checks are off until [code_host] labels are set" in out
    raw = (fake_home / ".pl-work-acme" / "config.toml").read_text()
    assert "sekrit" not in out and "sekrit" not in raw
    assert C.validate(t) == []


@pytest.mark.parametrize("drop,flag", [("--tracker", "--tracker"), ("--harness", "--harness"), ("--name", "--name")])
def test_missing_required_answer_exits_2_naming_the_flag(fake_home, monkeypatch, capsys, drop, flag):
    args = [*BASE, "--tracker", "github-issues", "--repo", "acme/app"]
    i = args.index(drop)
    del args[i:i + 2]
    with pytest.raises(SystemExit) as e:
        _pl(monkeypatch, *args)
    assert e.value.code == 2 and flag in capsys.readouterr().err
    assert not list(fake_home.glob(".pl-*"))


def test_missing_repo_for_github_issues_exits_2(fake_home, monkeypatch, capsys):
    with pytest.raises(SystemExit) as e:
        _pl(monkeypatch, *BASE, "--tracker", "github-issues")
    assert e.value.code == 2 and "--repo" in capsys.readouterr().err


EXTRA = '[tracker.tools.card]\ntool = "get_card"\n\n[loops.nightly]\nprompt = "/nightly"\n'


def _existing(home, monkeypatch, capsys, *extra):
    """A profile written by an earlier setup, plus keys setup never asks about. Returns its bytes."""
    gh = home / "ghw"
    gh.mkdir()
    (gh / "signed-in").write_text("")
    _exe(home / "notify", "#!/bin/sh\n")
    _pl(monkeypatch, *GITHUB_EXISTING, "--gh-config-dir", str(gh), "--attention-cmd", "~/notify", *extra)
    p = home / ".pl-work-acme" / "config.toml"
    p.write_text(p.read_text() + EXTRA)
    capsys.readouterr()
    return p.read_bytes()


def test_existing_profile_is_updated_with_a_backup_and_unknown_keys_kept(fake_home, monkeypatch, capsys):
    old = _existing(fake_home, monkeypatch, capsys)
    replaced = []
    real = os.replace
    monkeypatch.setattr(os, "replace", lambda a, b: (replaced.append((str(a), str(b))), real(a, b))[1])
    _pl(monkeypatch, "--yes", "--slug", "work-acme", "--repo", "acme/other")     # every other answer: the file's
    out = capsys.readouterr().out
    d = fake_home / ".pl-work-acme"
    assert "Profile 'work-acme' already exists; its current settings are the defaults below. Saving updates it; " \
           "the old config.toml is kept as config.toml.bak-" in out
    assert "updated ~/.pl-work-acme/config.toml" in out and "created ~/.pl-work-acme" not in out
    t = _cfg(fake_home, "work-acme")
    assert t["tracker"] == {"type": "github-project", "owner": "acme", "number": 3, "repo": "acme/other",
                            "status_field": "pl stage", "tools": {"card": {"tool": "get_card"}}}
    assert t["loops"]["nightly"] == {"prompt": "/nightly"} and t["name"] == "Work – Acme"
    assert t["code_host"]["gh_config_dir"] == "~/ghw" and t["paths"] == {"work_dir": "~/work", "attention_cmd": "~/notify"}
    assert t["accounts"] == {"claude": {"harness": "claude", "config_dir": "~/.claude"}}
    baks = list(d.glob("config.toml.bak-*"))
    assert len(baks) == 1 and baks[0].read_bytes() == old and stat.S_IMODE(baks[0].stat().st_mode) == 0o600
    assert re.fullmatch(r"config\.toml\.bak-\d{8}-\d{6}", baks[0].name)
    assert [b for a, b in replaced] == [str(d / "config.toml")] and stat.S_IMODE((d / "config.toml").stat().st_mode) == 0o600
    assert C.validate(t) == []


def test_existing_profile_interactive_enter_keeps_every_old_value(fake_home, monkeypatch, capsys):
    old = _existing(fake_home, monkeypatch, capsys)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    prompts = []
    monkeypatch.setattr("builtins.input", lambda p="": (prompts.append(p), "")[1])     # Enter at every prompt
    _pl(monkeypatch, "--slug", "work-acme")
    assert tomllib.loads(old.decode()) == _cfg(fake_home, "work-acme")
    shown = "\n".join(prompts)
    for v in ("[Work – Acme]", "[claude]", "[github-project]", "[acme]", "[3]", "[acme/app]", "[~/ghw]", "[~/work]",
              "[~/notify]"):
        assert v in shown, v
    assert "views" not in shown and "Sprint" in shown        # the views exist from the first run; Sprint is still asked


def test_existing_profile_validate_failure_restores_the_old_file_and_exits_3(fake_home, monkeypatch, capsys):
    old = _existing(fake_home, monkeypatch, capsys)
    monkeypatch.setattr(C, "validate", lambda doc: ["tracker.repo: bad"])
    with pytest.raises(SystemExit) as e:
        _pl(monkeypatch, "--yes", "--slug", "work-acme", "--repo", "acme/other")
    assert e.value.code == 3 and "tracker.repo" in capsys.readouterr().err
    assert (fake_home / ".pl-work-acme" / "config.toml").read_bytes() == old


def test_existing_folder_without_config_is_written(fake_home, monkeypatch, capsys):
    d = fake_home / ".pl-work-acme"
    d.mkdir()
    _pl(monkeypatch, *BASE, "--tracker", "github-issues", "--repo", "acme/app")
    out = capsys.readouterr().out
    assert "Profile 'work-acme' already exists; its current settings are the defaults below. Saving updates it." in out
    assert ".bak-" not in out and not list(d.glob("*.bak-*"))
    assert _cfg(fake_home, "work-acme")["tracker"] == {"type": "github-issues", "repo": "acme/app"}
    assert stat.S_IMODE((d / "config.toml").stat().st_mode) == 0o600


def test_profiles_new_still_refuses_an_existing_profile(fake_home, monkeypatch):
    (fake_home / ".pl-work-acme").mkdir()
    monkeypatch.setattr(sys, "argv", ["pl", "profiles", "new", "work-acme"])
    with pytest.raises(SystemExit, match="exists"):
        cli.main()
    assert list((fake_home / ".pl-work-acme").iterdir()) == []


def test_harness_not_on_path_prints_guidance_and_still_writes(fake_home, monkeypatch, capsys):
    args = ["--yes", "--name", "solo", "--harness", "codex", "--work-dir", "~/work", "--tracker", "github-issues", "--repo", "acme/app"]
    with pytest.raises(SystemExit) as e:           # no ~/.codex yet: refused before anything is written
        _pl(monkeypatch, *args)
    err = capsys.readouterr().err
    assert e.value.code == 2 and "--config-dir" in err and "~/.codex" in err and "sign in" in err
    assert not (fake_home / ".pl-solo").exists()
    (fake_home / ".codex").mkdir()
    with pytest.raises(SystemExit) as e:
        _pl(monkeypatch, *args)
    out = capsys.readouterr().out
    assert e.value.code == 3 and "ACTION NEEDED: install codex" in out
    assert "npm install -g @openai/codex" in out
    assert "setting up codex is outside pl; pl only uses it once you are signed in" in out
    t = _cfg(fake_home, "solo")
    assert t["accounts"] == {"codex": {"harness": "codex", "config_dir": "~/.codex"}}
    assert t["stages"]["run"]["account"] == "codex"


def test_separate_gh_folder_not_signed_in_prints_login_and_never_runs_it(fake_home, monkeypatch, capsys):
    gh = fake_home / "gh-new"
    gh.mkdir()
    with pytest.raises(SystemExit) as e:
        _pl(monkeypatch, *BASE, "--tracker", "github-issues", "--repo", "acme/app", "--gh-config-dir", str(gh))
    out = capsys.readouterr().out
    login = f"GH_CONFIG_DIR={gh} gh auth login -s project"
    assert e.value.code == 3 and login in out
    nxt = out[out.index("\nNext:"):]
    assert login in nxt and "ACTION NEEDED: " in nxt
    log = _gh_log(fake_home)
    assert f"GH_CONFIG_DIR={gh} auth status" in log and "auth login" not in log
    assert list(gh.iterdir()) == []
    assert _cfg(fake_home, "work-acme")["code_host"]["gh_config_dir"] == "~/gh-new"


def test_path_check_reports_a_foreign_pl_first_on_path(fake_home, monkeypatch, capsys):
    _exe(fake_home / "bin" / "pl", "#!/bin/sh\n")
    monkeypatch.setattr(sys, "prefix", str(fake_home / "tools" / "pl-funnel"))   # an installed tool, not a clone
    msg = setup.path_check(mine=fake_home / "venv" / "bin" / "pl")
    assert str(fake_home / "bin" / "pl") in msg and "uv tool update-shell" in msg
    with pytest.raises(SystemExit) as e:
        _pl(monkeypatch, *BASE, "--tracker", "github-issues", "--repo", "acme/app")
    out = capsys.readouterr().out
    assert e.value.code == 3 and f"ACTION NEEDED: The pl first on your PATH is {fake_home / 'bin' / 'pl'}" in out


def test_path_check_is_quiet_when_pl_is_this_install(fake_home):
    mine = fake_home / "venv" / "bin" / "pl"
    mine.parent.mkdir(parents=True)
    _exe(mine, "#!/bin/sh\n")
    os.symlink(mine, fake_home / "bin" / "pl")
    assert setup.path_check(mine=mine) is None


def test_path_check_from_a_clone_advises_installing_the_tool(fake_home, monkeypatch):
    monkeypatch.setattr(sys, "prefix", str(fake_home / "clone" / ".venv"))       # `uv run` inside a clone
    _exe(fake_home / "bin" / "uv", '#!/bin/sh\necho "uv $*" >> "$GH_LOG"\n')     # pl-funnel not installed as a tool
    msg = setup.path_check()
    assert f"uv tool install {fake_home / 'clone'}" in msg and "update-shell" not in msg
    assert "uv tool install" not in _gh_log(fake_home)                           # advice only, never run


def test_path_check_from_a_clone_asks_the_login_shell(fake_home, monkeypatch):
    monkeypatch.setattr(sys, "prefix", str(fake_home / "clone" / ".venv"))
    tools = fake_home / ".local" / "bin"
    _exe(fake_home / "bin" / "uv", f'#!/bin/sh\ncase "$*" in "tool list") echo "pl-funnel v0.1.0"; echo "- pl";;\n'
                                   f'  "tool dir --bin") echo "{tools}";; esac\n')
    _exe(fake_home / "loginsh", '#!/bin/sh\n[ "$1 $2" = "-lc command -v pl" ] && cat "$HOME/found"\n')
    monkeypatch.setenv("SHELL", str(fake_home / "loginsh"))
    (fake_home / "found").write_text("/usr/bin/pl\n")
    msg = setup.path_check()
    assert "/usr/bin/pl" in msg and "uv tool update-shell" in msg
    (fake_home / "found").write_text(f"{tools / 'pl'}\n")
    assert setup.path_check() is None


def test_eof_at_a_prompt_cancels_cleanly(fake_home, monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda p="": (_ for _ in ()).throw(EOFError))
    with pytest.raises(SystemExit, match="setup cancelled, nothing written"):
        _pl(monkeypatch)
    assert not list(fake_home.glob(".pl-*"))


def test_interactive_happy_path_uses_the_defaults(fake_home, monkeypatch, capsys):
    repo = fake_home / "work"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "git@github.com:acme/app.git"], check=True)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    answers = iter([
        "Home Lab",        # profile name
        "",                # slug: suggested home-lab
        "claude",          # harnesses
        "",                # claude config folder: ~/.claude
        "github-issues",   # where cards live
        "",                # repo: acme/app from the git remote
        "",                # gh sign-in: the default
        "",                # email: Enter skips it
        "edit",            # PR labels: anything but Enter asks each one
        "", "my-ready", "", "", "",   # review, ready, merge_ready, rework, failed
        "",                # work folder: this repo
        "",                # notifications: none
        "",                # create a notification script: yes
        "",                # use the local model: yes (Ollama and gemma4 are there, so nothing else is asked)
        "y",               # write it
        "",                # create the 5 missing labels: yes
        "n",               # run uv tool update-shell
    ])
    prompts = []
    monkeypatch.setattr("builtins.input", lambda p="": (prompts.append(p), next(answers))[1])
    _pl(monkeypatch)
    t = _cfg(fake_home, "home-lab")
    assert t["tracker"] == {"type": "github-issues", "repo": "acme/app"}
    assert t["paths"]["work_dir"] == "~/work" and t["accounts"]["claude"]["config_dir"] == "~/.claude"
    assert "uv tool update-shell" not in _gh_log(fake_home)
    assert any("home-lab" in p for p in prompts)
    assert t["paths"]["attention_cmd"] == "~/.pl-home-lab/notify" and "Create a notification script? [Y/n]: " in prompts
    assert C.validate(t) == []
    assert t["code_host"]["labels"] == {**{k: v[0] for k, v in setup.LABELS.items()}, "ready": "my-ready"}
    assert "Create 5 labels on acme/app? [Y/n]: " in prompts
    assert "label create my-ready --repo acme/app --color 0E8A16" in _gh_log(fake_home)
    assert t["user"] == {"login": "octo"}
    assert any("email" in p.lower() for p in prompts)


def test_pl_without_a_profile_points_at_setup(fake_home, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["pl", "list"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert "pl setup" in str(e.value)


@pytest.mark.parametrize("name", ["Status", " status ", "STATUS", "", "  "])
def test_status_field_blank_or_status_is_refused(fake_home, monkeypatch, capsys, name):
    with pytest.raises(SystemExit) as e:
        _pl(monkeypatch, *GITHUB_EXISTING, "--status-field", name)
    assert e.value.code == 2 and "--status-field" in capsys.readouterr().err
    assert not list(fake_home.glob(".pl-*")) and _writes(fake_home) == []


@pytest.mark.parametrize("tracker", [["--tracker", "github-issues", "--repo", "acme/app"], GITHUB_EXISTING[len(BASE):]])
def test_default_gh_sign_in_missing_asks_for_login(fake_home, monkeypatch, capsys, tracker):
    monkeypatch.setenv("GH_UNSIGNED", "1")
    with pytest.raises(SystemExit) as e:
        _pl(monkeypatch, *BASE, *tracker)
    out = capsys.readouterr().out
    assert e.value.code == 3 and "ACTION NEEDED: " in out and "gh auth login -s project" in out
    assert "GH_CONFIG_DIR=" not in out[out.index("ACTION NEEDED"):]
    assert "field-list" not in _gh_log(fake_home) and "auth login" not in _gh_log(fake_home)
    assert (fake_home / ".pl-work-acme" / "config.toml").exists()


def test_attention_cmd_must_be_an_executable_file(fake_home, monkeypatch, capsys):
    args = [*BASE, "--tracker", "github-issues", "--repo", "acme/app"]
    (fake_home / "notify").write_text("#!/bin/sh\n")                     # not executable
    for bad in ["/nonexistent/notify", str(fake_home / "notify")]:
        with pytest.raises(SystemExit) as e:
            _pl(monkeypatch, *args, "--attention-cmd", bad)
        assert e.value.code == 2 and "--attention-cmd" in capsys.readouterr().err
        assert not list(fake_home.glob(".pl-*"))
    _exe(fake_home / "notify", "#!/bin/sh\n")
    _pl(monkeypatch, *args, "--attention-cmd", "~/notify")
    assert _cfg(fake_home, "work-acme")["paths"]["attention_cmd"] == "~/notify"


def test_validate_failure_after_writing_exits_3_naming_the_key(fake_home, monkeypatch, capsys):
    monkeypatch.setattr(C, "validate", lambda doc: ["tracker.repo: bad"])
    with pytest.raises(SystemExit) as e:
        _pl(monkeypatch, *BASE, "--tracker", "github-issues", "--repo", "acme/app")
    assert e.value.code == 3 and "tracker.repo" in capsys.readouterr().err


def test_login_shell_path_check_reads_stdout_only_and_no_stdin(fake_home, monkeypatch):
    tools = fake_home / ".local" / "bin"
    _exe(fake_home / "loginsh", f'#!/bin/sh\nread x && echo "$x" > "$HOME/stolen"\necho "{tools / "pl"}"\n'
                                'echo "welcome from profile" >&2\n')
    r, w = os.pipe()
    os.write(w, b"typed-secret\n")
    os.close(w)
    saved = os.dup(0)
    os.dup2(r, 0)                                    # something waiting on stdin that the shell must not read
    try:
        msg = setup.path_check()
    finally:
        os.dup2(saved, 0)
        os.close(saved)
        os.close(r)
    assert msg is None and not (fake_home / "stolen").exists()


# ---------- WP18: PR labels ----------

DEFAULT_LABELS = {"review": "pl:auto-review", "ready": "pl:ready-for-review", "merge_ready": "pl:merge-ready",
                  "rework": "pl:needs-rework", "failed": "pl:review-failed"}
ISSUES = [*BASE, "--tracker", "github-issues", "--repo", "acme/app"]


def _label_calls(home):
    return [x.split(" ", 1)[1] for x in _gh_log(home).splitlines() if x.split(" ", 1)[1].startswith("label ")]


def test_labels_default_written_and_missing_ones_created_exactly(fake_home, monkeypatch, capsys):
    (fake_home / "labels.json").write_text(json.dumps([{"name": "pl:merge-ready"}, {"name": "bug"}]))
    _pl(monkeypatch, *ISSUES)
    out = capsys.readouterr().out
    assert _cfg(fake_home, "work-acme")["code_host"]["labels"] == DEFAULT_LABELS
    assert _label_calls(fake_home) == [
        "label list --repo acme/app --limit 500 --json name",
        "label create pl:auto-review --repo acme/app --color 1D76DB --description PR opened by a pl agent, waiting for auto review",
        "label create pl:ready-for-review --repo acme/app --color 0E8A16 --description Reviewed by pl, ready for a person",
        "label create pl:needs-rework --repo acme/app --color FBCA04 --description Needs more work before review",
        "label create pl:review-failed --repo acme/app --color B60205 --description Auto review gave up; needs a person"]
    assert "PR checks are off" not in out and "ACTION NEEDED: " not in out


def test_labels_use_the_chosen_gh_sign_in_folder(fake_home, monkeypatch, capsys):
    gh = fake_home / "ghw"
    gh.mkdir()
    (gh / "signed-in").write_text("")
    _pl(monkeypatch, *ISSUES, "--gh-config-dir", str(gh))
    lines = [x for x in _gh_log(fake_home).splitlines() if " label " in x]
    assert len(lines) == 6 and all(x.startswith(f"GH_CONFIG_DIR={gh} label ") for x in lines)


def test_one_label_flag_overrides_one_label(fake_home, monkeypatch, capsys):
    _pl(monkeypatch, *ISSUES, "--label-ready", "reviewed", "--no-create-labels")
    assert _cfg(fake_home, "work-acme")["code_host"]["labels"] == {**DEFAULT_LABELS, "ready": "reviewed"}
    assert _label_calls(fake_home) == []


def test_existing_profile_keeps_its_labels_as_defaults(fake_home, monkeypatch, capsys):
    _existing(fake_home, monkeypatch, capsys, "--label-rework", "rework-me", "--label-failed", "gave-up")
    _pl(monkeypatch, "--yes", "--slug", "work-acme", "--label-review", "auto")
    assert _cfg(fake_home, "work-acme")["code_host"]["labels"] == {
        **DEFAULT_LABELS, "review": "auto", "rework": "rework-me", "failed": "gave-up"}


def test_labels_not_signed_in_prints_the_commands_and_creates_nothing(fake_home, monkeypatch, capsys):
    monkeypatch.setenv("GH_UNSIGNED", "1")
    with pytest.raises(SystemExit) as e:
        _pl(monkeypatch, *ISSUES)
    out = capsys.readouterr().out
    assert e.value.code == 3 and _label_calls(fake_home) == []
    todo = out[out.index("ACTION NEEDED"):]
    assert "gh label create pl:auto-review --repo acme/app --color 1D76DB --description " \
           "'PR opened by a pl agent, waiting for auto review'" in todo
    assert "gh label create pl:review-failed --repo acme/app --color B60205" in todo
    assert _cfg(fake_home, "work-acme")["code_host"]["labels"] == DEFAULT_LABELS


def test_no_labels_writes_none_and_keeps_the_old_message(fake_home, monkeypatch, capsys):
    _pl(monkeypatch, *ISSUES, "--no-labels")
    out = capsys.readouterr().out
    assert "labels" not in _cfg(fake_home, "work-acme")["code_host"]
    assert "[code_host.labels]" not in (fake_home / ".pl-work-acme" / "config.toml").read_text()
    assert "PR checks are off until [code_host] labels are set" in out and _label_calls(fake_home) == []


@pytest.mark.parametrize("flag,value", [("--label-review", "-x"), ("--label-failed", "--repo=evil/x"),
                                        ("--label-ready", "a,b"), ("--label-rework", " ")])
def test_a_label_gh_could_read_as_a_flag_is_refused(fake_home, monkeypatch, capsys, flag, value):
    with pytest.raises(SystemExit) as e:
        _pl(monkeypatch, *ISSUES, f"{flag}={value}")
    assert e.value.code == 2 and flag in capsys.readouterr().err
    assert not list(fake_home.glob(".pl-*")) and _label_calls(fake_home) == []


# ---------- WP21: [user] from the GitHub sign-in ----------

def test_github_tracker_writes_user_login_from_the_gh_sign_in_and_no_email(fake_home, monkeypatch, capsys):
    _pl(monkeypatch, *BASE, "--tracker", "github-issues", "--repo", "acme/app")
    t = _cfg(fake_home, "work-acme")
    assert t["user"] == {"login": "octo"} and C.validate(t) == []


def test_user_login_comes_from_the_chosen_separate_gh_folder(fake_home, monkeypatch, capsys):
    gh = fake_home / "ghw"
    gh.mkdir()
    (gh / "signed-in").write_text("")
    _pl(monkeypatch, *GITHUB_EXISTING, "--gh-config-dir", str(gh))
    assert _cfg(fake_home, "work-acme")["user"]["login"] == "octo"


def test_email_flag_is_optional_and_written(fake_home, monkeypatch, capsys):
    _pl(monkeypatch, *BASE, "--tracker", "github-issues", "--repo", "acme/app", "--email", "me@example.com")
    assert _cfg(fake_home, "work-acme")["user"] == {"login": "octo", "email": "me@example.com"}


def test_existing_profile_keeps_its_user_values(fake_home, monkeypatch, capsys):
    _existing(fake_home, monkeypatch, capsys, "--email", "me@example.com")
    p = fake_home / ".pl-work-acme" / "config.toml"
    p.write_text(p.read_text().replace('[user]\n', '[user]\nname = "Me"\n'))
    _pl(monkeypatch, "--yes", "--slug", "work-acme")
    assert _cfg(fake_home, "work-acme")["user"] == {"name": "Me", "login": "octo", "email": "me@example.com"}


def test_github_project_setup_creates_the_start_label_and_says_the_defaults_are_on(fake_home, monkeypatch, capsys):
    (fake_home / "labels.json").write_text(json.dumps([]))
    _pl(monkeypatch, *GITHUB_EXISTING)
    out = capsys.readouterr().out
    assert "label create pl:start --repo acme/app --color C2E0C6 --description Adopt this issue into the pl funnel" \
        in _label_calls(fake_home)
    assert "issue intake and auto-review loop on" in out


# ---------- WP41: the generated notification script ----------

GITHUB_ISSUES = [*BASE, "--tracker", "github-issues", "--repo", "acme/app"]
NASTY = ['Plan "ready": $(touch /tmp/pl-pwned) `id`', "it's done; $HOME \\ \"quoted\""]


def test_setup_writes_the_notify_script_and_points_attention_cmd_at_it(fake_home, monkeypatch, capsys):
    _pl(monkeypatch, *GITHUB_ISSUES)
    script = fake_home / ".pl-work-acme" / "notify"
    assert stat.S_IMODE(script.stat().st_mode) == 0o700
    assert script.read_text().startswith("#!/bin/sh\n")
    assert _cfg(fake_home, "work-acme")["paths"]["attention_cmd"] == "~/.pl-work-acme/notify"
    assert C.validate(_cfg(fake_home, "work-acme")) == []


def test_no_notify_script_flag_writes_none(fake_home, monkeypatch):
    _pl(monkeypatch, *GITHUB_ISSUES, "--no-notify-script")
    assert not (fake_home / ".pl-work-acme" / "notify").exists()
    assert "attention_cmd" not in _cfg(fake_home, "work-acme").get("paths", {})


def test_an_existing_notify_file_is_kept_without_force(fake_home, monkeypatch, capsys):
    stamps = iter(range(10))
    monkeypatch.setattr(setup.time, "strftime", lambda _f: f"stamp-{next(stamps)}")   # one backup per run
    _pl(monkeypatch, *GITHUB_ISSUES, "--no-notify-script")   # the profile exists, without a script
    script = fake_home / ".pl-work-acme" / "notify"
    _exe(script, "#!/bin/sh\n# mine\n")
    _pl(monkeypatch, *GITHUB_ISSUES, "--slug", "work-acme", "--notify-script")   # the merge path offers it too
    assert script.read_text() == "#!/bin/sh\n# mine\n" and str(script) in capsys.readouterr().out.replace("~", str(fake_home))
    assert _cfg(fake_home, "work-acme")["paths"]["attention_cmd"] == "~/.pl-work-acme/notify"
    _pl(monkeypatch, *GITHUB_ISSUES, "--slug", "work-acme", "--notify-script", "--force")
    assert script.read_text() == setup.NOTIFY_SCRIPT and stat.S_IMODE(script.stat().st_mode) == 0o700


def _run_script(tmp_path, system, tools):
    """Run the generated script with only fakes on PATH; each fake records its argv NUL-separated."""
    bin_ = tmp_path / "sbin"
    bin_.mkdir()
    _exe(bin_ / "uname", f"#!/bin/sh\necho {system}\n")
    for t in tools:
        _exe(bin_ / t, f"#!/bin/sh\nprintf '%s\\0' \"$@\" > {tmp_path}/{t}.argv\n/bin/cat > {tmp_path}/{t}.stdin\n")
    script = tmp_path / "notify"
    _exe(script, setup.NOTIFY_SCRIPT)
    r = subprocess.run([str(script), "notify", *NASTY], capture_output=True, text=True,
                       env={"PATH": str(bin_), "HOME": str(tmp_path)})
    argv = {t: (tmp_path / f"{t}.argv").read_text().split("\0")[:-1] for t in tools if (tmp_path / f"{t}.argv").exists()}
    return r, argv


def test_script_on_macos_passes_title_and_message_to_osascript_as_argv(tmp_path):
    r, argv = _run_script(tmp_path, "Darwin", ["osascript", "notify-send"])
    assert r.returncode == 0 and argv["osascript"][:3] == ["-", *NASTY]
    assert "on run argv" in (tmp_path / "osascript.stdin").read_text() and "notify-send" not in argv
    assert not os.path.exists("/tmp/pl-pwned")


def test_script_on_linux_uses_notify_send(tmp_path):
    r, argv = _run_script(tmp_path, "Linux", ["osascript", "notify-send"])
    assert r.returncode == 0 and argv["notify-send"][-2:] == NASTY and "osascript" not in argv


def test_script_with_no_notifier_rings_the_bell_on_stderr(tmp_path):
    r, argv = _run_script(tmp_path, "Linux", [])
    assert r.returncode == 0 and r.stderr.startswith("\a") and NASTY[0] in r.stderr and NASTY[1] in r.stderr


def test_setup_writes_machine_toml_with_the_manager_on(fake_home, monkeypatch, capsys):
    from pl import manager
    _pl(monkeypatch, *GITHUB_EXISTING)
    f = fake_home / ".local" / "state" / "pl-machine" / "machine.toml"
    assert f.read_text() == manager.MACHINE_TOML and manager.enabled() is True
    assert "machine.toml" in capsys.readouterr().out


def test_setup_says_the_manager_is_off_when_machine_toml_turns_it_off(fake_home, monkeypatch, capsys):
    f = fake_home / ".local" / "state" / "pl-machine" / "machine.toml"
    f.parent.mkdir(parents=True)
    f.write_text("[manager]\nenabled = false\n")
    _pl(monkeypatch, *GITHUB_EXISTING)
    out = capsys.readouterr().out
    assert "kept" in out and "the manager is off" in out and "runs every profile" not in out


# ---------- built-in stage skills ----------

BUILTIN = {"spec": "/pl-spec {id}", "design": "/pl-design {id}", "plan": "/pl-plan {id}", "run": "/pl-run {id}"}


def test_a_new_profile_uses_the_builtin_stage_skills_and_setup_installs_them(fake_home, monkeypatch, capsys):
    _pl(monkeypatch, *BASE, "--tracker", "github-issues", "--repo", "acme/app")
    t = _cfg(fake_home, "work-acme")
    assert {s: v["prompt"] for s, v in t["stages"].items()} == BUILTIN
    assert {s: v["account"] for s, v in t["stages"].items()} == {s: "claude" for s in BUILTIN}
    lib = fake_home / ".local" / "share" / "pl" / "skills"
    assert sorted(p.name for p in lib.iterdir() if not p.name.startswith(".")) == \
        ["pl-design", "pl-plan", "pl-review", "pl-run", "pl-spec"]
    assert "installed built-in pl-spec" in capsys.readouterr().out


def _custom_prompts(fake_home):
    import tomlkit
    p = fake_home / ".pl-work-acme" / "config.toml"
    doc = tomlkit.parse(p.read_text())
    for s in ("spec", "plan", "run"):
        doc["stages"][s]["prompt"] = f"/my-{s} {{id}}"
    del doc["stages"]["design"]["prompt"]
    p.write_text(tomlkit.dumps(doc))
    return p.read_bytes()


def test_an_existing_profile_keeps_its_prompts_until_use_builtin_stages(fake_home, monkeypatch, capsys):
    _pl(monkeypatch, *BASE, "--tracker", "github-issues", "--repo", "acme/app")
    _custom_prompts(fake_home)
    _pl(monkeypatch, "--yes", "--slug", "work-acme")                       # a re-run never touches prompts
    old = (fake_home / ".pl-work-acme" / "config.toml").read_bytes()
    assert _cfg(fake_home, "work-acme")["stages"]["spec"]["prompt"] == "/my-spec {id}"
    capsys.readouterr()
    _pl(monkeypatch, "--use-builtin-stages", "--slug", "work-acme", "--yes")
    out = capsys.readouterr().out
    t = _cfg(fake_home, "work-acme")
    assert {s: v["prompt"] for s, v in t["stages"].items()} == BUILTIN
    assert t["stages"]["spec"]["account"] == "claude" and C.validate(t) == []
    baks = sorted((fake_home / ".pl-work-acme").glob("config.toml.bak-*"))
    assert baks[-1].read_bytes() == old and stat.S_IMODE(baks[-1].stat().st_mode) == 0o600
    assert "spec: /my-spec {id} -> /pl-spec {id}" in out and "design: (none) -> /pl-design {id}" in out
    assert f"backup: {baks[-1].name}" in out
    capsys.readouterr()
    _pl(monkeypatch, "--use-builtin-stages", "--slug", "work-acme", "--yes")   # already switched: nothing to write
    assert "already use the built-in stage skills" in capsys.readouterr().out
    assert len(list((fake_home / ".pl-work-acme").glob("config.toml.bak-*"))) == len(baks)


def test_a_rerun_keeps_a_stage_accounts_pool(fake_home, monkeypatch):
    import tomlkit
    _pl(monkeypatch, *BASE, "--tracker", "github-issues", "--repo", "acme/app")
    p = fake_home / ".pl-work-acme" / "config.toml"
    doc = tomlkit.parse(p.read_text())
    del doc["stages"]["run"]["account"]
    doc["stages"]["run"]["accounts"] = ["claude"]
    p.write_text(tomlkit.dumps(doc))
    _pl(monkeypatch, "--yes", "--slug", "work-acme")
    t = _cfg(fake_home, "work-acme")
    assert t["stages"]["run"]["accounts"] == ["claude"] and "account" not in t["stages"]["run"]
    assert t["stages"]["spec"]["account"] == "claude"


def test_use_builtin_stages_needs_an_existing_profile(fake_home, monkeypatch):
    with pytest.raises(SystemExit, match="no profile"):
        _pl(monkeypatch, "--use-builtin-stages", "--slug", "nope", "--yes")


def test_use_builtin_stages_checks_the_slug(fake_home, monkeypatch):
    with pytest.raises(SystemExit, match="lower-case"):
        _pl(monkeypatch, "--use-builtin-stages", "--slug", "../evil", "--yes")


# ---------- the optional local model (Ollama + Gemma 4) ----------

class _Ollama:
    """Fakes for the local model step: brew and ollama calls are recorded, never run; which, the platform and what
    Ollama answers (a set of pulled models per check, None = not answering) are set per test."""

    def __init__(self):
        self.calls, self.results = [], {}
        self.which = {"ollama": "/usr/local/bin/ollama", "brew": None}
        self.platform, self.models = "linux", [{"gemma4:latest"}]


@pytest.fixture(autouse=True)
def ollama(monkeypatch):
    """Every setup test: a new profile turns the local model on by default, so no test reaches brew, ollama or a server."""
    import shutil
    from pl import local_model
    st = _Ollama()
    real_run, real_which = setup._run, shutil.which

    def run(argv, env=None, stdout_only=False, timeout=60, live=False):
        if argv[0] not in ("brew", "ollama"):
            return real_run(argv, env=env, stdout_only=stdout_only)
        st.calls.append((argv, live))
        r = st.results.get(" ".join(argv[:2]), (0, ""))
        if argv[:2] == ["brew", "install"] and r[0] == 0:
            st.which["ollama"] = "/opt/homebrew/bin/ollama"
        return r

    monkeypatch.setattr(setup, "_run", run)
    monkeypatch.setattr(shutil, "which", lambda n, *a, **k: st.which[n] if n in st.which else real_which(n, *a, **k))
    monkeypatch.setattr(setup, "_platform", lambda: st.platform)
    monkeypatch.setattr(setup, "_sleep", lambda s: None)
    monkeypatch.setattr(local_model, "models", lambda s=None: st.models.pop(0) if len(st.models) > 1 else st.models[0])
    return st


def _argvs(st):
    return [" ".join(a) for a, _ in st.calls]


NEW_INTERACTIVE = ["--name", "Work – Acme", "--harness", "claude", "--work-dir", "~/work", "--tracker", "github-issues",
                   "--repo", "acme/app", "--no-notify-script", "--no-create-labels"]


def test_a_new_profile_turns_it_on_by_default_with_yes(fake_home, monkeypatch, capsys, ollama):
    _pl(monkeypatch, *GITHUB_ISSUES)
    t = _cfg(fake_home, "work-acme")
    assert t["local_model"] == {"enabled": True, "model": "gemma4"} and C.validate(t) == []
    assert ollama.calls == [] and "gemma4 is pulled" in capsys.readouterr().out


def test_a_new_profile_is_asked_and_yes_is_the_default(fake_home, monkeypatch, capsys, ollama):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    prompts = []
    monkeypatch.setattr("builtins.input", lambda p="": (prompts.append(p), "")[1])
    _pl(monkeypatch, *NEW_INTERACTIVE)
    assert any("Gemma 4" in p and "[Y/n]" in p and "stays on this machine" in p for p in prompts)
    assert _cfg(fake_home, "work-acme")["local_model"]["enabled"] is True


def test_no_at_the_prompt_writes_nothing_and_runs_nothing(fake_home, monkeypatch, capsys, ollama):
    ollama.which["ollama"] = None
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda p="": "n" if "Gemma 4" in p else "")
    _pl(monkeypatch, *NEW_INTERACTIVE)
    assert "local_model" not in _cfg(fake_home, "work-acme") and ollama.calls == []
    assert "ollama.com" not in capsys.readouterr().out


def test_a_missing_model_is_pulled_with_progress_shown(fake_home, monkeypatch, capsys, ollama):
    ollama.models = [set()]
    _pl(monkeypatch, *GITHUB_ISSUES)
    out = capsys.readouterr().out
    assert ollama.calls == [(["ollama", "pull", "gemma4"], True)]
    assert "several GB" in out


def test_the_download_prompt_warns_and_can_be_declined(fake_home, monkeypatch, capsys, ollama):
    ollama.models = [set()]
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    prompts = []
    monkeypatch.setattr("builtins.input", lambda p="": (prompts.append(p), "n" if "ollama pull" in p else "")[1])
    _pl(monkeypatch, *NEW_INTERACTIVE)
    assert any("several GB" in p and "[Y/n]" in p for p in prompts) and ollama.calls == []
    assert _cfg(fake_home, "work-acme")["local_model"]["enabled"] is True


def test_a_failed_pull_is_a_warning_not_a_setup_failure(fake_home, monkeypatch, capsys, ollama):
    ollama.models = [set()]
    ollama.results["ollama pull"] = (1, "")
    _pl(monkeypatch, *GITHUB_ISSUES)   # no SystemExit
    out = capsys.readouterr().out
    assert "warning" in out and "ollama pull gemma4" in out and "ACTION NEEDED: " not in out
    assert _cfg(fake_home, "work-acme")["local_model"]["enabled"] is True


def test_missing_ollama_on_macos_is_installed_with_brew(fake_home, monkeypatch, capsys, ollama):
    ollama.which.update(ollama=None, brew="/opt/homebrew/bin/brew")
    ollama.platform = "darwin"
    _pl(monkeypatch, *GITHUB_ISSUES)
    assert _argvs(ollama)[0] == "brew install ollama" and ollama.calls[0][1] is True


def test_a_failed_brew_install_is_a_warning(fake_home, monkeypatch, capsys, ollama):
    ollama.which.update(ollama=None, brew="/opt/homebrew/bin/brew")
    ollama.platform = "darwin"
    ollama.results["brew install"] = (1, "")
    _pl(monkeypatch, *GITHUB_ISSUES)   # no SystemExit
    out = capsys.readouterr().out
    assert _argvs(ollama) == ["brew install ollama"]
    assert "warning" in out and "https://ollama.com/download" in out


def test_missing_ollama_without_brew_prints_the_download_page_and_runs_nothing(fake_home, monkeypatch, capsys, ollama):
    ollama.which["ollama"] = None
    _pl(monkeypatch, *GITHUB_ISSUES)
    out = capsys.readouterr().out
    assert "https://ollama.com/download" in out and "ollama pull gemma4" in out and "| sh" not in out
    assert ollama.calls == [] and _cfg(fake_home, "work-acme")["local_model"]["enabled"] is True


def test_a_stopped_server_installed_by_brew_is_started_with_brew_services(fake_home, monkeypatch, capsys, ollama):
    ollama.which["brew"] = "/opt/homebrew/bin/brew"
    ollama.platform = "darwin"
    ollama.results["brew list"] = (0, "ollama 0.12.0\n")
    ollama.models = [None, {"gemma4:latest"}]
    _pl(monkeypatch, *GITHUB_ISSUES)
    assert _argvs(ollama) == ["brew list --versions ollama", "brew services start ollama"]


def test_a_stopped_server_elsewhere_says_how_to_start_it(fake_home, monkeypatch, capsys, ollama):
    ollama.models = [None]
    _pl(monkeypatch, *GITHUB_ISSUES)
    out = capsys.readouterr().out
    assert "ollama serve" in out and ollama.calls == []


def test_an_existing_profile_is_not_asked_and_keeps_its_section(fake_home, monkeypatch, capsys, ollama):
    _existing(fake_home, monkeypatch, capsys, "--no-local-model")
    p = fake_home / ".pl-work-acme" / "config.toml"
    p.write_text(p.read_text() + '\n[local_model]\nenabled = true\nmodel = "gemma4:e4b"\ntimeout = 60\n')
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    prompts = []
    monkeypatch.setattr("builtins.input", lambda p="": (prompts.append(p), "")[1])
    _pl(monkeypatch, "--slug", "work-acme")
    assert not any("Gemma 4" in x for x in prompts) and ollama.calls == []
    assert _cfg(fake_home, "work-acme")["local_model"] == {"enabled": True, "model": "gemma4:e4b", "timeout": 60}


def test_an_existing_profile_without_the_section_gets_none(fake_home, monkeypatch, capsys, ollama):
    _existing(fake_home, monkeypatch, capsys, "--no-local-model")
    _pl(monkeypatch, "--yes", "--slug", "work-acme")
    assert "local_model" not in _cfg(fake_home, "work-acme") and ollama.calls == []


def test_flags_change_an_existing_profile(fake_home, monkeypatch, capsys, ollama):
    _existing(fake_home, monkeypatch, capsys, "--no-local-model")
    _pl(monkeypatch, "--yes", "--slug", "work-acme", "--local-model")
    assert _cfg(fake_home, "work-acme")["local_model"] == {"enabled": True, "model": "gemma4"}
    for b in (fake_home / ".pl-work-acme").glob("config.toml.bak-*"):   # a second save in the same second
        b.unlink()
    _pl(monkeypatch, "--yes", "--slug", "work-acme", "--no-local-model")
    assert _cfg(fake_home, "work-acme")["local_model"] == {"enabled": False, "model": "gemma4"}


def test_no_local_model_on_a_new_profile_writes_nothing(fake_home, monkeypatch, capsys, ollama):
    _pl(monkeypatch, *GITHUB_ISSUES, "--no-local-model")
    assert "local_model" not in _cfg(fake_home, "work-acme") and ollama.calls == []
