"""Built-in stage skills: shipped in the package, copied into the skills library, never over a copy you edited,
reset on request, and every pl command they name exists. Temp HOME; nothing real is touched."""
import argparse
import re
import shlex
import shutil
import sys
from pathlib import Path

import pytest

from pl import board, cli, commands, harnesses, skills
from pl import config as C

NAMES = ["pl-design", "pl-plan", "pl-review", "pl-run", "pl-spec"]
CLI = Path(cli.__file__).read_text()


@pytest.fixture
def lib(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(C, "SKILLS", {}, raising=False)
    monkeypatch.setattr(C, "PROFILES", {}, raising=False)
    monkeypatch.setattr(C, "STATE_DIR", tmp_path / "state", raising=False)
    return tmp_path / ".local" / "share" / "pl" / "skills"


@pytest.fixture
def shipped(tmp_path, monkeypatch):
    """A copy of the package's built-ins that a test may change, standing in for a newer pl release."""
    d = tmp_path / "pkg"
    shutil.copytree(skills.BUILTIN_DIR, d)
    monkeypatch.setattr(skills, "BUILTIN_DIR", d)
    return d


def _bump(shipped, name, text="\nNew step.\n"):
    f = shipped / name / "SKILL.md"
    f.write_text(f.read_text().replace("pl-builtin-version: 1", "pl-builtin-version: 2") + text)


# ---------- the package ----------

def test_the_package_ships_five_skills_with_frontmatter():
    assert skills.builtin_names() == NAMES
    for n in NAMES:
        text = (skills.BUILTIN_DIR / n / "SKILL.md").read_text()
        head = text.split("---\n")[1]
        assert f"name: {n}\n" in head and re.search(r"^description: \S.{20,}$", head, re.M)
        assert re.search(r"^pl-builtin-version: \d+$", head, re.M)
        assert len(text.splitlines()) <= 150, n


def _pl_commands():
    """Every command and sub-command cli.py defines: add_parser names plus the ones main() runs before parsing."""
    top = set(re.findall(r'\bsub\.add_parser\("([a-z-]+)"', CLI)) | set(re.findall(r'rest\[:1\] == \["([a-z-]+)"\]', CLI))
    return top, {"skills": set(re.findall(r'\bpk\.add_parser\("([a-z-]+)"', CLI))}


def _mentions(text):
    """`pl <cmd> [<sub>]` inside inline code and fenced code blocks."""
    code = re.findall(r"```.*?```", text, re.S) + re.findall(r"`[^`\n]+`", re.sub(r"```.*?```", "", text, flags=re.S))
    return [m for c in code for m in re.findall(r"(?<![\w./-])pl ([a-z][a-z-]*)(?: ([a-z][a-z-]*))?", c)]


def test_every_pl_command_a_skill_names_exists_in_the_cli():
    top, subs = _pl_commands()
    assert {"card", "move", "section", "approve", "reject", "intent", "setup"} <= top and "reset" in subs["skills"]
    seen = set()
    for n in NAMES:
        for cmd, sub in _mentions((skills.BUILTIN_DIR / n / "SKILL.md").read_text()):
            seen.add(cmd)
            assert cmd in top, f"{n}: pl {cmd}"
            assert cmd not in subs or not sub or sub in subs[cmd], f"{n}: pl {cmd} {sub}"
    assert {"card", "move", "section"} <= seen


def test_the_mention_parser_catches_a_made_up_command():
    assert ("frobnicate", "") in _mentions("run `pl frobnicate <id>`")
    assert ("skills", "nope") in _mentions("```\npl skills nope X\n```")
    assert _mentions("the pl pipeline and `pl-spec` and `/pl:pl-run`") == []


# ---------- install ----------

def test_install_copies_every_builtin_into_the_library(lib):
    out = skills.install_builtins()
    assert sorted(p.name for p in lib.iterdir() if not p.name.startswith(".")) == NAMES
    for n in NAMES:
        assert (lib / n / "SKILL.md").read_bytes() == (skills.BUILTIN_DIR / n / "SKILL.md").read_bytes()
    assert len(out) == 5 and all(m.startswith("installed built-in ") for m in out)
    assert skills.install_builtins() == []                       # a second run changes nothing
    assert {i["name"] for i in skills.scan() if i["account"] == "library"} == set(NAMES)


def test_an_unedited_older_copy_is_updated(lib, shipped):
    skills.install_builtins()
    _bump(shipped, "pl-spec")
    assert skills.install_builtins() == ["updated built-in pl-spec to version 2"]
    assert (lib / "pl-spec" / "SKILL.md").read_text().endswith("New step.\n")


def test_an_edited_copy_is_never_overwritten(lib, shipped):
    skills.install_builtins()
    mine = (lib / "pl-spec" / "SKILL.md").read_text() + "\nMy own rule.\n"
    (lib / "pl-spec" / "SKILL.md").write_text(mine)
    _bump(shipped, "pl-spec")
    assert skills.install_builtins() == ["built-in pl-spec has an update; your edited copy was kept"]
    assert (lib / "pl-spec" / "SKILL.md").read_text() == mine
    assert skills.install_builtins() == []                       # said once per new version, not on every start
    _bump(shipped, "pl-plan")
    assert skills.install_builtins() == ["updated built-in pl-plan to version 2"]
    assert (lib / "pl-spec" / "SKILL.md").read_text() == mine


def test_a_users_own_skill_of_that_name_is_kept(lib):
    (lib / "pl-run").mkdir(parents=True)
    (lib / "pl-run" / "SKILL.md").write_text("---\nname: pl-run\ndescription: mine\n---\nmine\n")
    out = skills.install_builtins()
    assert "built-in pl-run has an update; your edited copy was kept" in out
    assert (lib / "pl-run" / "SKILL.md").read_text().endswith("mine\n")


def test_a_link_in_the_library_is_never_written_through(lib, tmp_path):
    lib.mkdir(parents=True)
    target = tmp_path / "elsewhere"
    target.mkdir()
    (lib / "pl-spec").symlink_to(target, target_is_directory=True)
    skills.install_builtins()
    assert list(target.iterdir()) == []


# ---------- reset ----------

def _reset(monkeypatch, *args, answer=None):
    if answer is not None:
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda _p="": answer)
    return skills.cmd_skills(argparse.Namespace(skills_cmd="reset", name=args[0], yes="--yes" in args))


def test_reset_restores_the_shipped_version_after_a_yes(lib, monkeypatch, capsys):
    skills.install_builtins()
    f = lib / "pl-plan" / "SKILL.md"
    f.write_text("my edit\n")
    _reset(monkeypatch, "pl-plan", answer="n")
    assert f.read_text() == "my edit\n" and "nothing changed" in capsys.readouterr().out
    _reset(monkeypatch, "pl-plan", answer="y")
    assert f.read_bytes() == (skills.BUILTIN_DIR / "pl-plan" / "SKILL.md").read_bytes()
    baks = list(f.parent.glob("SKILL.md.bak-*"))
    assert len(baks) == 1 and baks[0].read_text() == "my edit\n"
    assert "restored built-in pl-plan" in capsys.readouterr().out
    assert skills.install_builtins() == []                       # the reset copy counts as unedited again


def test_reset_without_a_terminal_needs_yes(lib, monkeypatch):
    skills.install_builtins()
    (lib / "pl-spec" / "SKILL.md").write_text("x\n")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    with pytest.raises(SystemExit, match="--yes"):
        _reset(monkeypatch, "pl-spec")
    assert (lib / "pl-spec" / "SKILL.md").read_text() == "x\n"
    _reset(monkeypatch, "pl-spec", "--yes")
    assert "x\n" != (lib / "pl-spec" / "SKILL.md").read_text()


def test_reset_refuses_a_name_that_is_not_built_in(lib, monkeypatch):
    with pytest.raises(SystemExit, match="not a built-in"):
        _reset(monkeypatch, "alpha", "--yes")


def test_pl_skills_reset_is_a_cli_command(lib, tmp_path, monkeypatch, capsys):
    prof = tmp_path / ".pl-t"
    prof.mkdir()
    (prof / "config.toml").write_text("")
    monkeypatch.setenv("PL_CONFIG_DIR", str(prof))
    skills.install_builtins()
    (lib / "pl-run" / "SKILL.md").write_text("x\n")
    monkeypatch.setattr(sys, "argv", ["pl", "skills", "reset", "pl-run", "--yes"])
    cli.main()
    assert (lib / "pl-run" / "SKILL.md").read_bytes() == (skills.BUILTIN_DIR / "pl-run" / "SKILL.md").read_bytes()


# ---------- the stage prompts reach every harness ----------

def test_builtin_stage_prompts_name_the_skills():
    assert C.BUILTIN_PROMPTS == {"spec": "/pl-spec {id}", "design": "/pl-design {id}", "plan": "/pl-plan {id}",
                                 "run": "/pl-run {id}"}


def test_a_loop_prompt_of_a_library_skill_gets_the_plugin_name_on_claude(lib, monkeypatch):
    skills.install_builtins()
    monkeypatch.setattr(C, "PROFILES", {"a": lib.parent / "acct"})
    monkeypatch.setattr(C, "ACCOUNTS", {"a": {"harness": "claude"}})
    (lib.parent / "acct").mkdir()
    argv = shlex.split(harnesses.launch_script(harnesses.get("claude"), "a", "/loop 30m /pl-review repo=o/r", "s",
                                               "l").splitlines()[-1])
    assert argv[-1] == "/loop 30m /pl:pl-review repo=o/r"
    argv = shlex.split(harnesses.launch_script(harnesses.get("claude"), "a", "/pl-spec abc", "s", "l").splitlines()[-1])
    assert argv[-1] == "/pl:pl-spec abc"


def test_the_builtin_review_loop_uses_pl_review(tmp_path, monkeypatch):
    lab = {"review": "pl:auto-review", "ready": "pl:ready", "rework": "pl:rework", "failed": "pl:failed"}
    p = C._review_prompt("acme/app", lab, {"harness": "claude"})
    assert p == "/loop 30m /pl-review repo=acme/app review=pl:auto-review ready=pl:ready rework=pl:rework failed=pl:failed"
    assert C._review_prompt("acme/app", lab, {"harness": "codex"}).startswith("/pl-review repo=acme/app ")
    text = (skills.BUILTIN_DIR / "pl-review" / "SKILL.md").read_text()
    for rule in ("remove `<review>`, add `<ready>`", "add both `<ready>` and `<rework>`", "add `<failed>`", "Never merge"):
        assert rule in text


# ---------- pl section: read and write one card section ----------

@pytest.fixture
def card(monkeypatch):
    c = {"id": "c" * 36, "title": "T", "list_id": "l1",
         "description": board.render({"INPUT": "the idea", "SPEC": "old spec"})}
    sent = []
    monkeypatch.setattr(commands, "find_card", lambda token: dict(c))
    monkeypatch.setattr(commands, "fresh_next", lambda: None)
    monkeypatch.setattr(commands, "update", lambda cid, **f: (sent.append((cid, f)), c.update(f))[1])
    return c, sent


def _section(id_, name, from_=None):
    return commands.cmd_section(argparse.Namespace(id=id_, name=name, from_=from_))


def test_section_prints_one_section_in_full(card, capsys):
    _section("cccccccc", "INPUT")
    assert capsys.readouterr().out == "the idea\n"
    _section("cccccccc", "DESIGN")
    r = capsys.readouterr()
    assert r.out == "" and "no DESIGN section" in r.err


def test_section_writes_one_section_and_keeps_the_others(card, tmp_path):
    c, sent = card
    f = tmp_path / "spec.md"
    f.write_text("## Problem\nnew spec\n")
    _section("cccccccc", "SPEC", str(f))
    parts = board.sections(c["description"])
    assert parts == {"INPUT": "the idea", "SPEC": "## Problem\nnew spec"} and len(sent) == 1


@pytest.mark.parametrize("name,text,why", [("INPUT", "x", "SPEC, DESIGN or PLAN"),
                                           ("SPEC", "a\n# PIPELINE: PLAN\nb", "# PIPELINE:"),
                                           ("SPEC", "  \n", "empty")])
def test_section_write_refuses_input_marker_lines_and_empty_text(card, tmp_path, name, text, why):
    f = tmp_path / "x.md"
    f.write_text(text)
    with pytest.raises(SystemExit, match=re.escape(why)):
        _section("cccccccc", name, str(f))
    assert card[1] == []


# ---------- pl move --pr records the pull request ----------

@pytest.fixture
def moved(monkeypatch):
    meta = {"pr_urls": ["https://github.com/o/r/pull/1"]}
    sent = []

    class T:
        def ref_id(self, ref):
            return "o/r#7"

        def move(self, ref, column):
            sent.append(("move", ref, column))
            return "o/r#7"
    monkeypatch.setattr(commands.trackers, "get", lambda kind: T())
    monkeypatch.setattr(commands, "fresh_next", lambda: None)
    monkeypatch.setattr(commands, "card", lambda cid: {"id": cid, "metadata": dict(meta)})
    monkeypatch.setattr(commands, "update", lambda cid, **f: (sent.append(("update", cid, f)), meta.update(f["metadata"])))
    return meta, sent


def _move(pr):
    commands.cmd_move(argparse.Namespace(id="7", column="PR open", pr=pr))


def test_move_with_pr_appends_the_url_once(moved):
    meta, sent = moved
    _move("https://github.com/o/r/pull/2")
    _move("https://github.com/o/r/pull/2")
    assert meta["pr_urls"] == ["https://github.com/o/r/pull/1", "https://github.com/o/r/pull/2"]
    assert [s[0] for s in sent] == ["update", "move", "move"]


@pytest.mark.parametrize("url", ["http://github.com/o/r/pull/2", "https://github.com/o/r/issues/2",
                                 "https://evil.test/o/r/pull/2", "https://github.com/o/r/pull/2x", "o/r#2"])
def test_move_with_pr_rejects_anything_but_a_github_pull_url_before_moving(moved, url):
    with pytest.raises(SystemExit, match="pull request URL"):
        _move(url)
    assert moved[1] == []


def test_move_without_pr_writes_no_metadata(moved):
    _move(None)
    assert [s[0] for s in moved[1]] == ["move"]


def test_pl_run_records_its_pr_through_pl_move():
    assert 'pl move <id> "PR open" --pr <pull request URL>' in (skills.BUILTIN_DIR / "pl-run" / "SKILL.md").read_text()


# ---------- the dispatcher installs the built-ins; the review loop falls back when pl-review is missing ----------

class _Stop(Exception):
    pass


def _start_dispatcher(monkeypatch, install):
    from pl import dispatch, events, manager, profiles
    passes = []
    monkeypatch.setattr(manager, "manages", lambda name: False)
    monkeypatch.setattr(dispatch, "dispatch_lock", lambda: object())
    monkeypatch.setattr(events, "emit", lambda *a, **k: None)
    monkeypatch.setattr(profiles, "shared_warnings", lambda *a: [])
    monkeypatch.setattr(profiles, "list_profiles", lambda: [])
    monkeypatch.setattr(profiles, "current_row", lambda: None)
    monkeypatch.setattr(skills, "install_builtins", install)
    monkeypatch.setattr(dispatch, "dispatch_once", lambda *a: (passes.append(a), (_ for _ in ()).throw(_Stop()))[1])
    with pytest.raises(_Stop):
        dispatch.cmd_dispatch(argparse.Namespace(once=True, dry_run=False, no_pull=True, max_runs=None, max_prep=None,
                                                 interval=None))
    return passes


def test_the_dispatcher_installs_the_builtins_at_start(lib, monkeypatch):
    calls = []
    assert _start_dispatcher(monkeypatch, lambda: calls.append(1) or ["installed built-in pl-review"])
    assert calls == [1]


def test_a_failed_install_never_stops_the_dispatcher(lib, monkeypatch):
    def boom():
        raise OSError("disk full")
    assert _start_dispatcher(monkeypatch, boom)          # the first pass still ran


def test_the_review_loop_falls_back_to_the_inline_prompt_while_pl_review_is_missing(lib, monkeypatch):
    lab = {"review": "pl:auto-review", "ready": "pl:ready", "rework": "pl:rework", "failed": "pl:failed"}
    svc = {"prompt": C._review_prompt("acme/app", lab, {"harness": "claude"}),
           "fallback": C._review_text("acme/app", lab, {"harness": "claude"}), "builtin": True}
    assert svc["fallback"].startswith("/loop 30m Review pull requests on acme/app.")
    assert "Never merge" in svc["fallback"]
    from pl import dispatch
    assert dispatch.loop_prompt(svc) == svc["fallback"]
    skills.install_builtins()
    assert dispatch.loop_prompt(svc) == svc["prompt"]
    assert dispatch.loop_prompt({"prompt": "/mine"}) == "/mine"


# ---------- review round 1 ----------

def _section_f(id_, name, from_=None, force=False):
    return commands.cmd_section(argparse.Namespace(id=id_, name=name, from_=from_, force=force))


@pytest.fixture
def col(monkeypatch):
    where = {"col": "Plan for review"}
    monkeypatch.setattr(commands, "col_name", lambda list_id: where["col"])
    return where


@pytest.mark.parametrize("column", ["Approved", "In progress", "PR open", "Done"])
def test_section_refuses_the_plan_once_approved_unless_forced(card, col, tmp_path, column):
    col["col"] = column
    f = tmp_path / "plan.md"
    f.write_text("new plan\n")
    with pytest.raises(SystemExit, match="approved"):
        _section_f("cccccccc", "PLAN", str(f))
    assert card[1] == []
    _section_f("cccccccc", "PLAN", str(f), force=True)
    assert board.sections(card[0]["description"])["PLAN"] == "new plan"


def test_section_writes_the_plan_before_approval(card, col, tmp_path):
    f = tmp_path / "plan.md"
    f.write_text("p\n")
    _section_f("cccccccc", "PLAN", str(f))
    assert len(card[1]) == 1


def test_section_refuses_an_approved_spec_unless_forced(card, col, tmp_path):
    card[0]["metadata"] = {"spec_approved_at": "2026-01-01T00:00:00Z"}
    col["col"] = "Spec ready"
    f = tmp_path / "s.md"
    f.write_text("s\n")
    with pytest.raises(SystemExit, match="approved"):
        _section_f("cccccccc", "SPEC", str(f))
    assert card[1] == []
    _section_f("cccccccc", "SPEC", str(f), force=True)
    assert len(card[1]) == 1


def test_section_force_is_a_cli_flag():
    assert '"--force"' in CLI.split('add_parser("section")')[1].split("add_parser(")[0]


def test_section_refuses_nul_bytes(card, col, tmp_path):
    f = tmp_path / "s.md"
    f.write_bytes(b"a\x00b\n")
    with pytest.raises(SystemExit, match="NUL"):
        _section_f("cccccccc", "SPEC", str(f))
    assert card[1] == []


@pytest.fixture
def acct(tmp_path, monkeypatch):
    d = tmp_path / "acct"
    (d / "skills" / "x").mkdir(parents=True)
    (d / "auth.json").write_text('{"token": "secret"}')
    (d / "settings.json").write_text("{}")
    (d / "skills" / "x" / "SKILL.md").write_text("a skill\n")
    monkeypatch.setattr(C, "PROFILES", {"a": d}, raising=False)
    return d


def test_section_from_refuses_credential_files_links_and_account_config(card, col, acct, tmp_path):
    link = tmp_path / "spec.md"
    link.symlink_to(acct / "auth.json")
    hard = tmp_path / "plain.md"
    os_link = __import__("os").link
    os_link(acct / "auth.json", hard)
    for src in (acct / "auth.json", link, hard, acct / "settings.json"):
        with pytest.raises(SystemExit, match="refused"):
            _section_f("cccccccc", "SPEC", str(src))
    assert card[1] == []
    _section_f("cccccccc", "SPEC", str(acct / "skills" / "x" / "SKILL.md"))   # skills/ is fine
    assert len(card[1]) == 1


def test_section_from_refuses_files_over_256_kb(card, col, tmp_path):
    f = tmp_path / "big.md"
    f.write_text("x" * (256 * 1024 + 1))
    with pytest.raises(SystemExit, match="256 KB"):
        _section_f("cccccccc", "SPEC", str(f))
    assert card[1] == []


def test_move_with_pr_reads_fresh_and_records_before_moving(moved, monkeypatch):
    calls = []
    monkeypatch.setattr(commands, "fresh_next", lambda: calls.append("fresh"))
    monkeypatch.setattr(commands, "card", lambda cid: (calls.append(("card", cid)), {"id": cid, "metadata": {}})[1])
    _move("https://github.com/o/r/pull/3")
    assert calls == ["fresh", ("card", "o/r#7")]
    assert [s[0] for s in moved[1]] == ["update", "move"]


def test_a_config_error_from_install_never_stops_the_dispatcher(lib, monkeypatch, capsys):
    def bad():
        raise SystemExit("pl: config: [skills] library 'x' must be an absolute path")
    assert _start_dispatcher(monkeypatch, bad)
    assert "must be an absolute path" in capsys.readouterr().err


def test_a_config_error_from_install_never_stops_the_console(monkeypatch):
    from pl.tui import app
    said = []

    def bad():
        raise SystemExit("pl: config: bad library")
    monkeypatch.setattr(skills, "install_builtins", bad)
    app.PlApp.install_builtins(argparse.Namespace(notify=lambda m, **k: said.append(m)))
    assert len(said) == 1 and "bad library" in said[0]


def test_a_broken_state_file_never_crashes_install(lib, shipped):
    import json
    skills.install_builtins()
    st = json.loads((lib / ".pl-builtin.json").read_text())
    st["pl-spec"] = "junk"
    st["pl-plan"] = {**st["pl-plan"], "version": "abc", "told": "x"}
    st["pl-run"] = ["x"]
    (lib / ".pl-builtin.json").write_text(json.dumps(st))
    _bump(shipped, "pl-plan")
    out = skills.install_builtins()
    assert "updated built-in pl-plan to version 2" in out
    (lib / ".pl-builtin.json").write_text('["not", "a", "dict"]')
    skills.install_builtins()


def test_an_update_that_fails_mid_write_leaves_the_old_copy_whole(lib, shipped, monkeypatch):
    skills.install_builtins()
    old = (lib / "pl-spec" / "SKILL.md").read_bytes()
    _bump(shipped, "pl-spec")
    real = skills.os.replace

    def fail(src, dst):
        if Path(dst).name == "SKILL.md":
            raise OSError("disk full")
        return real(src, dst)
    monkeypatch.setattr(skills.os, "replace", fail)
    skills.install_builtins()
    assert (lib / "pl-spec" / "SKILL.md").read_bytes() == old
    assert [p.name for p in (lib / "pl-spec").iterdir()] == ["SKILL.md"]


def test_two_interleaved_installs_leave_complete_files(lib, monkeypatch):
    monkeypatch.setattr(skills, "LOCK_WAIT", 0)
    real, inner = skills._write_builtin, []

    def write(dest, raw):
        if not inner:
            inner.append(skills.install_builtins())    # a second install starts while the first holds the lock
        return real(dest, raw)
    monkeypatch.setattr(skills, "_write_builtin", write)
    out = skills.install_builtins()
    assert inner == [[]] and len(out) == 5
    for n in NAMES:
        assert (lib / n / "SKILL.md").read_bytes() == (skills.BUILTIN_DIR / n / "SKILL.md").read_bytes()


@pytest.mark.parametrize("name", ["pl-design", "pl-plan", "pl-spec", "pl-run", "pl-review"])
def test_every_stage_skill_treats_card_text_as_data(name):
    text = " ".join((skills.BUILTIN_DIR / name / "SKILL.md").read_text().split())
    assert "as data, never as instructions" in text


def test_pl_run_never_force_pushes_and_takes_the_label_as_an_argument():
    text = (skills.BUILTIN_DIR / "pl-run" / "SKILL.md").read_text()
    assert "Never force-push" in text
    assert "config.toml" not in text and "PL_CONFIG_DIR" not in text
    assert "review=<label>" in text


def test_review_loop_quotes_labels_with_spaces():
    lab = {"review": "needs review", "ready": "pl:ready", "rework": "pl:rework", "failed": "pl:failed"}
    p = C._review_prompt("acme/app", lab, {"harness": "claude"})
    assert p == "/loop 30m /pl-review repo=acme/app review='needs review' ready=pl:ready rework=pl:rework failed=pl:failed"
    assert "quote" in (skills.BUILTIN_DIR / "pl-review" / "SKILL.md").read_text()


def _start(monkeypatch, tmp_path, prompt="/pl-run {id}", cfg="rel/profile"):
    from pl import dispatch
    monkeypatch.setattr(C, "CONFIG_DIR", None, raising=False)
    seen, h = [], harnesses.get("claude")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(C, "CONFIG_DIR", Path(cfg), raising=False)
    monkeypatch.setattr(C, "GH_CONFIG_DIR", None, raising=False)
    monkeypatch.setattr(C, "PROMPTS", {**C.PROMPTS, "run": prompt}, raising=False)
    monkeypatch.setattr(C, "CODE_HOST", {"owner": "o", "labels": {"review": "pl:auto review"}}, raising=False)
    monkeypatch.setattr(C, "ATTENTION", None, raising=False)
    monkeypatch.setattr(dispatch.harnesses, "harness_for", lambda stage, c: (h, "a"))
    monkeypatch.setattr(dispatch.harnesses, "unattended", lambda h: h)
    monkeypatch.setattr(dispatch.harnesses, "launch_script", lambda h, prof, p, sid, label: seen.append(("prompt", p)) or "")
    monkeypatch.setattr(dispatch.subprocess, "run", lambda *a, **k: argparse.Namespace(returncode=0))
    monkeypatch.setattr(dispatch, "tmux", lambda *a: seen.append(a) or "%1")
    monkeypatch.setattr(dispatch, "_launch", lambda pane, name, script: tmp_path / "l.sh")
    monkeypatch.setattr(dispatch, "update", lambda *a, **k: None)
    monkeypatch.setattr(dispatch.events, "emit", lambda *a, **k: None)
    dispatch.start_worker({"id": "c" * 36, "title": "T", "description": ""}, "run", 0, False)
    return seen


def test_agent_windows_get_an_absolute_pl_config_dir(monkeypatch, tmp_path):
    seen = _start(monkeypatch, tmp_path)
    nw = next(a for a in seen if a[0] == "new-window")
    i = nw.index(f"PL_CONFIG_DIR={tmp_path.resolve() / 'rel' / 'profile'}")
    assert nw[i - 1] == "-e"


def test_the_builtin_run_stage_gets_the_review_label(monkeypatch, tmp_path):
    seen = _start(monkeypatch, tmp_path)
    assert ("prompt", f"/pl-run {'c' * 36} review='pl:auto review'") in seen
    seen = _start(monkeypatch, tmp_path, prompt="/my-run {id}")
    assert ("prompt", f"/my-run {'c' * 36}") in seen


def test_a_bare_loop_prompt_of_a_library_skill_gets_the_plugin_name(lib, monkeypatch):
    skills.install_builtins()
    monkeypatch.setattr(C, "PROFILES", {"a": lib.parent / "acct"})
    monkeypatch.setattr(C, "ACCOUNTS", {"a": {"harness": "claude"}})
    (lib.parent / "acct").mkdir()
    argv = shlex.split(harnesses.launch_script(harnesses.get("claude"), "a", "/loop /pl-review repo=o/r", "s",
                                               "l").splitlines()[-1])
    assert argv[-1] == "/loop /pl:pl-review repo=o/r"
