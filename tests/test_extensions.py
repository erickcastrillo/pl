"""WP12: a harness account's skills, slash commands and hooks are listed, opened, created and copied."""
import builtins
import contextlib
import io
import json
import os
from pathlib import Path

import pytest
from rich.console import Console
from textual.app import App
from textual.widgets import DataTable, Input, OptionList

from pl import config as C
from pl import harnesses
from pl.tui import extensions
from pl.tui.app import PlApp

SKILL = "---\nname: {n}\ndescription: {d}\n---\nBody\n"


def make_skill(root, name, desc="Does a thing"):
    d = root / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(SKILL.format(n=name, d=desc))
    return d


def make_claude(root):
    root.mkdir(parents=True)
    make_skill(root, "alpha", "A" * 300)
    make_skill(root, "beta", "Second skill")
    (root / "commands").mkdir()
    (root / "commands" / "deploy.md").write_text("Deploy the thing\n")
    (root / "settings.json").write_text(json.dumps({
        "model": "secret-model", "env": {"TOKEN": "s3cret"},
        "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "./check.sh"}]}]}}))
    (root / ".credentials.json").write_text('{"token": "do-not-read"}')
    (root / ".claude.json").write_text("{}")
    return root


@pytest.fixture
def accounts(tmp_path, monkeypatch):
    dirs = {"a": make_claude(tmp_path / ".claude-a"), "b": tmp_path / ".claude-b", "c": tmp_path / ".codex-c"}
    dirs["b"].mkdir()
    dirs["c"].mkdir()
    (dirs["c"] / "prompts").mkdir()
    (dirs["c"] / "prompts" / "review.md").write_text("Review it\n")
    (dirs["c"] / "auth.json").write_text('{"token": "do-not-read"}')
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(C, "PROFILES", dict(dirs))
    monkeypatch.setattr(C, "ACCOUNTS", {"a": {"harness": "claude", "config_dir": str(dirs["a"])},
                                        "b": {"harness": "claude", "config_dir": str(dirs["b"])},
                                        "c": {"harness": "codex", "config_dir": str(dirs["c"])}})
    return dirs


CLAUDE, CODEX, AGY = (harnesses.get(n) for n in ("claude", "codex", "antigravity"))


# ---------- extensions() ----------

def test_claude_lists_skills_commands_and_hooks_with_summaries(accounts):
    ext = harnesses.extensions(CLAUDE, "a")
    assert sorted(s["name"] for s in ext["skills"]) == ["alpha", "beta"]
    alpha = next(s for s in ext["skills"] if s["name"] == "alpha")
    assert alpha["summary"] == "A" * 120 and Path(alpha["path"]) == accounts["a"] / "skills/alpha/SKILL.md"
    assert [c["name"] for c in ext["commands"]] == ["deploy"]
    assert Path(ext["commands"][0]["path"]) == accounts["a"] / "commands/deploy.md"
    assert len(ext["hooks"]) == 1 and "PreToolUse" in ext["hooks"][0]["name"] and "Bash" in ext["hooks"][0]["name"]
    assert Path(ext["hooks"][0]["path"]) == accounts["a"] / "settings.json"
    assert not ext.get("error")


def test_malformed_settings_json_gives_an_error_string_and_no_crash(accounts):
    (accounts["a"] / "settings.json").write_text("{not json")
    ext = harnesses.extensions(CLAUDE, "a")
    assert ext["hooks"] == [] and isinstance(ext["error"], str) and "settings.json" in ext["error"]
    assert len(ext["skills"]) == 2


def test_codex_lists_prompts_only_and_marks_the_rest_unsupported(accounts):
    ext = harnesses.extensions(CODEX, "c")
    assert [c["name"] for c in ext["commands"]] == ["review"]
    assert ext["skills"] == [] and ext["hooks"] == []
    assert ext["supported"] == {"skills": False, "commands": True, "hooks": False}


def test_antigravity_supports_nothing(accounts):
    ext = harnesses.extensions(AGY, "c")
    assert ext["skills"] == ext["commands"] == ext["hooks"] == []
    assert ext["supported"] == {"skills": False, "commands": False, "hooks": False}


def test_a_skill_symlinked_outside_the_config_dir_is_not_listed_or_read(accounts, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "SKILL.md").write_text(SKILL.format(n="evil", d="steals"))
    (accounts["a"] / "skills" / "evil").symlink_to(outside)
    assert "evil" not in [s["name"] for s in harnesses.extensions(CLAUDE, "a")["skills"]]


def test_only_the_extension_paths_are_ever_opened(accounts, tmp_path, monkeypatch):
    opened = []
    real = builtins.open
    real_os_open = os.open

    def spy(file, *a, **k):
        opened.append(os.path.realpath(file) if isinstance(file, (str, os.PathLike)) else str(file))
        return real(file, *a, **k)
    monkeypatch.setattr(builtins, "open", spy)
    monkeypatch.setattr(io, "open", spy)

    def os_spy(file, *a, **k):
        opened.append(os.path.realpath(file))
        return real_os_open(file, *a, **k)
    monkeypatch.setattr(os, "open", os_spy)
    for h, acct in ((CLAUDE, "a"), (CODEX, "c"), (AGY, "c")):
        harnesses.extensions(h, acct)
    extensions.create(CLAUDE, "b", "skills", "new-one")
    extensions.copy(CLAUDE, "a", "skills", "alpha", "b")
    extensions.copy(CLAUDE, "a", "commands", "deploy", "b")
    allowed = {"skills", "commands", "prompts", "settings.json"}
    root = os.path.realpath(tmp_path)
    mine = [Path(p) for p in opened if str(p).startswith(root)]
    assert mine, "the scan opened nothing: the recorder is not seeing reads"
    for p in mine:
        rel = p.relative_to(root).parts
        assert rel[1] in allowed, f"opened {p}"
    assert not any(p.name in (".credentials.json", ".claude.json", "auth.json") for p in mine)


def test_links_to_credential_files_are_never_listed_opened_or_copied(accounts, monkeypatch):
    a, c = accounts["a"], accounts["c"]
    (a / "skills/s1").mkdir()
    (a / "skills/s1/SKILL.md").symlink_to(a / ".credentials.json")
    (a / "commands/c1.md").symlink_to(a / ".credentials.json")
    (a / "skills/whole").symlink_to(a)
    (a / "skills/up").symlink_to(a.parent)
    (c / "prompts/p1.md").symlink_to(c / "auth.json")
    (a / ".credentials.json").write_text("---\ndescription: do-not-read\n---\n")
    opened = []
    real = builtins.open

    def spy(file, *args, **kw):
        opened.append(os.path.realpath(file))
        return real(file, *args, **kw)
    real_os_open = os.open

    def os_spy(file, *args, **kw):
        opened.append(os.path.realpath(file))
        return real_os_open(file, *args, **kw)
    monkeypatch.setattr(builtins, "open", spy)
    monkeypatch.setattr(os, "open", os_spy)
    ext, cext = harnesses.extensions(CLAUDE, "a"), harnesses.extensions(CODEX, "c")
    assert sorted(s["name"] for s in ext["skills"]) == ["alpha", "beta"]
    assert [x["name"] for x in ext["commands"]] == ["deploy"] and [x["name"] for x in cext["commands"]] == ["review"]
    assert "do-not-read" not in json.dumps([ext, cext])
    assert not any(Path(p).name in (".credentials.json", "auth.json") for p in opened), opened
    for kind, name in (("skills", "whole"), ("skills", "up"), ("skills", "s1"), ("commands", "c1")):
        with pytest.raises(SystemExit):
            extensions.copy(CLAUDE, "a", kind, name, "b")
    assert not any(p.name == ".credentials.json" or p.is_symlink() for p in accounts["b"].rglob("*"))


def test_a_skills_dir_linked_to_another_accounts_skills_is_listed(accounts):
    (accounts["b"] / "skills").symlink_to(accounts["a"] / "skills")
    assert sorted(s["name"] for s in harnesses.extensions(CLAUDE, "b")["skills"]) == ["alpha", "beta"]
    assert extensions.missing_skill("/alpha {id}", CLAUDE, "b") is None
    assert extensions.missing_skill("/nope {id}", CLAUDE, "b") == "nope"



def _case_insensitive(p):
    return (p.parent / p.name.upper()).exists()


def test_case_variants_of_a_credential_file_or_config_dir_are_refused(accounts, tmp_path):
    a = accounts["a"]
    if not _case_insensitive(a):
        pytest.skip("needs a case-insensitive file system")
    (a / "skills/cv0").mkdir()
    (a / "skills/cv0/SKILL.md").symlink_to(a / ".CREDENTIALS.JSON")
    (a / "commands/cv0.md").symlink_to(a / ".Credentials.Json")
    ext = harnesses.extensions(CLAUDE, "a")
    assert "cv0" not in [s["name"] for s in ext["skills"] + ext["commands"]]
    assert "do-not-read" not in json.dumps(ext)
    make_skill(a, "cv1")
    (a / "skills/cv1/k").symlink_to(a / ".CREDENTIALS.JSON")
    make_skill(a, "cv2")
    (a / "skills/cv2/up").symlink_to(tmp_path / a.name.upper())
    for name in ("cv1", "cv2"):
        with pytest.raises(SystemExit):
            extensions.copy(CLAUDE, "a", "skills", name, "b")
    assert not (accounts["b"] / "skills").exists() or not list((accounts["b"] / "skills").iterdir())


def test_hard_links_to_a_credential_file_are_never_listed_read_or_copied(accounts):
    a, c = accounts["a"], accounts["c"]
    (a / "skills/h1").mkdir()
    os.link(a / ".credentials.json", a / "skills/h1/SKILL.md")
    os.link(a / ".credentials.json", a / "commands/h2.md")
    os.link(c / "auth.json", c / "prompts/h4.md")
    ext, cext = harnesses.extensions(CLAUDE, "a"), harnesses.extensions(CODEX, "c")
    assert "do-not-read" not in json.dumps([ext, cext])
    assert "h1" not in [s["name"] for s in ext["skills"]] and "h2" not in [x["name"] for x in ext["commands"]]
    assert "h4" not in [x["name"] for x in cext["commands"]]
    make_skill(a, "h3")
    os.link(a / ".credentials.json", a / "skills/h3/notes.json")
    for kind, name in (("skills", "h3"), ("commands", "h2")):
        with pytest.raises(SystemExit):
            extensions.copy(CLAUDE, "a", kind, name, "b")
    assert not any("do-not-read" in p.read_text() for p in accounts["b"].rglob("*") if p.is_file())


def test_a_file_swapped_for_a_link_after_the_check_is_not_read(accounts, monkeypatch):
    a = accounts["a"]
    orig = harnesses.real_path

    def racy(root, p, shape):
        r = orig(root, p, shape)
        if r is not None and r.name == "SKILL.md" and r.parent.name == "alpha":
            r.unlink()
            r.symlink_to(a / ".credentials.json")
        return r
    monkeypatch.setattr(harnesses, "real_path", racy)
    ext = harnesses.extensions(CLAUDE, "a")
    assert "do-not-read" not in json.dumps(ext)
    assert [s["name"] for s in ext["skills"]] == ["beta"]


# ---------- create ----------

def test_create_writes_a_skill_and_a_command_from_a_template(accounts):
    p = extensions.create(CLAUDE, "b", "skills", "my-skill")
    assert Path(p) == accounts["b"] / "skills/my-skill/SKILL.md"
    text = p.read_text()
    assert text.startswith("---\n") and "name: my-skill" in text and "description:" in text
    q = extensions.create(CLAUDE, "b", "commands", "ship-it")
    assert Path(q) == accounts["b"] / "commands/ship-it.md" and q.read_text().strip()
    assert [s["name"] for s in harnesses.extensions(CLAUDE, "b")["skills"]] == ["my-skill"]


@pytest.mark.parametrize("name", ["", "..", "../x", "a/b", "A", "-a", ".hidden", "a b", "a\n", "x/../y"])
def test_create_refuses_bad_names_and_writes_nothing(accounts, name):
    before = sorted(p for p in accounts["b"].rglob("*"))
    with pytest.raises(SystemExit):
        extensions.create(CLAUDE, "b", "skills", name)
    with pytest.raises(SystemExit):
        extensions.create(CLAUDE, "b", "commands", name)
    assert sorted(p for p in accounts["b"].rglob("*")) == before
    assert not list(accounts["b"].parent.glob("x*"))


def test_create_refuses_an_existing_name_without_touching_it(accounts):
    before = (accounts["a"] / "skills/alpha/SKILL.md").read_text()
    with pytest.raises(SystemExit) as e:
        extensions.create(CLAUDE, "a", "skills", "alpha")
    assert "exists" in str(e.value)
    with pytest.raises(SystemExit):
        extensions.create(CLAUDE, "a", "commands", "deploy")
    assert (accounts["a"] / "skills/alpha/SKILL.md").read_text() == before


def test_create_refuses_a_skills_dir_that_is_a_symlink_elsewhere(accounts, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (accounts["b"] / "skills").symlink_to(elsewhere)
    with pytest.raises(SystemExit):
        extensions.create(CLAUDE, "b", "skills", "sneaky")
    assert list(elsewhere.iterdir()) == []


def test_create_refuses_unsupported_kinds(accounts):
    with pytest.raises(SystemExit) as e:
        extensions.create(CODEX, "c", "skills", "x")
    assert "not supported by codex yet" in str(e.value)
    with pytest.raises(SystemExit):
        extensions.create(CLAUDE, "a", "hooks", "x")


# ---------- copy ----------

def test_copy_a_skill_folder_and_a_command_file_to_another_account(accounts):
    link = accounts["a"] / "skills/alpha/link"
    outside = accounts["a"].parent / "outside.txt"
    outside.write_text("private")
    link.symlink_to(outside)
    (accounts["a"] / "skills/alpha/notes.txt").write_text("more")
    extensions.copy(CLAUDE, "a", "skills", "alpha", "b")
    extensions.copy(CLAUDE, "a", "commands", "deploy", "b")
    assert (accounts["b"] / "skills/alpha/SKILL.md").read_text() == (accounts["a"] / "skills/alpha/SKILL.md").read_text()
    assert (accounts["b"] / "skills/alpha/notes.txt").read_text() == "more"
    assert (accounts["b"] / "skills/alpha/link").is_symlink()      # copied as a link, never followed
    assert (accounts["b"] / "commands/deploy.md").read_text() == "Deploy the thing\n"
    assert (accounts["a"] / "skills/alpha/SKILL.md").exists()       # a copy, not a move


def test_copy_refuses_existing_name_same_account_and_unsupported_target(accounts):
    extensions.copy(CLAUDE, "a", "skills", "alpha", "b")
    (accounts["b"] / "skills/alpha/SKILL.md").write_text("mine")
    with pytest.raises(SystemExit) as e:
        extensions.copy(CLAUDE, "a", "skills", "alpha", "b")
    assert "exists" in str(e.value) and (accounts["b"] / "skills/alpha/SKILL.md").read_text() == "mine"
    with pytest.raises(SystemExit) as e:
        extensions.copy(CLAUDE, "a", "skills", "beta", "a")
    assert "same account" in str(e.value)
    with pytest.raises(SystemExit) as e:
        extensions.copy(CLAUDE, "a", "skills", "beta", "c")
    assert "not supported by codex yet" in str(e.value)
    assert not (accounts["c"] / "skills").exists()
    with pytest.raises(SystemExit):
        extensions.copy(CLAUDE, "a", "hooks", "PreToolUse Bash", "b")


def test_copy_refuses_a_target_skills_dir_that_is_a_symlink_elsewhere(accounts, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (accounts["b"] / "skills").symlink_to(elsewhere)
    with pytest.raises(SystemExit):
        extensions.copy(CLAUDE, "a", "skills", "alpha", "b")
    assert list(elsewhere.iterdir()) == []


def test_copy_refuses_a_claude_command_into_a_codex_account(accounts):
    with pytest.raises(SystemExit) as e:
        extensions.copy(CLAUDE, "a", "commands", "deploy", "c")
    assert "within one harness" in str(e.value)
    assert not (accounts["c"] / "prompts/deploy.md").exists()


@pytest.mark.parametrize("kind,name", [("skills", "../skills/alpha"), ("commands", "../commands/deploy")])
def test_copy_refuses_a_bad_name_even_when_it_points_at_a_real_source(accounts, kind, name):
    with pytest.raises(SystemExit) as e:
        extensions.copy(CLAUDE, "a", kind, name, "b")
    assert "not a valid name" in str(e.value)
    assert not (accounts["b"] / "skills").exists() and not (accounts["b"] / "commands").exists()


def test_copy_refuses_a_source_skill_linked_outside_every_account(accounts, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "SKILL.md").write_text(SKILL.format(n="ext", d="outside"))
    (accounts["a"] / "skills/ext").symlink_to(outside)
    with pytest.raises(SystemExit):
        extensions.copy(CLAUDE, "a", "skills", "ext", "b")
    assert not (accounts["b"] / "skills/ext").exists()


def test_a_command_that_is_a_link_is_copied_as_a_link(accounts):
    other = accounts["b"] / "commands"
    other.mkdir()
    (other / "tool.md").write_text("Shared tool\n")
    (accounts["a"] / "commands/shared.md").symlink_to(other / "tool.md")
    dest = extensions.copy(CLAUDE, "a", "commands", "shared", "b")
    assert dest.is_symlink() and dest.read_text() == "Shared tool\n"


# ---------- stage prompt warning ----------

def test_missing_skill_warns_only_when_the_account_lacks_it(accounts):
    assert extensions.missing_skill("/missing-skill {id}", CLAUDE, "a") == "missing-skill"
    assert extensions.missing_skill("/alpha {id}", CLAUDE, "a") is None
    assert extensions.missing_skill("/deploy {id}", CLAUDE, "a") is None        # a command counts
    assert extensions.missing_skill("Write a spec for {id}", CLAUDE, "a") is None
    assert extensions.missing_skill("/anything {id}", CODEX, "c") is None       # no skills there: nothing to check


# ---------- the screen ----------

def screen_text(app):
    console = Console(width=app.size.width, height=app.size.height, file=io.StringIO(), record=True,
                      force_terminal=True, color_system="truecolor", legacy_windows=False, safe_box=False)
    console.print(app.screen._compositor.render_update(full=True))
    return console.export_text()


async def settle(pilot):
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()
    await pilot.pause()


class Host(App):
    def compose(self):
        yield extensions.ExtensionsView()


def rows(app, table):
    t = app.query_one(f"#{table}", DataTable)
    return [str(t.get_row_at(i)[0]) for i in range(t.row_count)]


async def test_screen_lists_all_three_kinds_and_repeats_the_subscription_line(accounts):
    app = Host()
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        assert rows(app, "ext-skills") == ["alpha", "beta"]
        assert rows(app, "ext-commands") == ["deploy"]
        assert len(rows(app, "ext-hooks")) == 1
        text = " ".join(screen_text(app).split())
        assert "on your own subscription. It never asks for or stores an API key." in text
        assert "Does a thing" in text or "AAAA" in text        # detail of the selected skill


async def test_unsupported_kinds_say_so(accounts):
    app = Host()
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        app.query_one(extensions.ExtensionsView).show("c")
        await settle(pilot)
        assert "not supported by codex yet" in " ".join(screen_text(app).split())
        assert rows(app, "ext-commands") == ["review"]


async def test_n_asks_for_a_name_and_creates_the_skill(accounts):
    app = Host()
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        app.query_one("#ext-skills").focus()
        await pilot.press("n")
        await settle(pilot)
        app.screen.query_one(Input).value = "fresh-skill"
        await pilot.press("enter")
        await settle(pilot)
        assert (accounts["a"] / "skills/fresh-skill/SKILL.md").exists()
        assert "fresh-skill" in rows(app, "ext-skills")


async def test_n_with_a_bad_name_creates_nothing_and_keeps_running(accounts):
    app = Host()
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        app.query_one("#ext-skills").focus()
        await pilot.press("n")
        await settle(pilot)
        app.screen.query_one(Input).value = "../x"
        await pilot.press("enter")
        await settle(pilot)
        assert app.is_running and app._exception is None
        assert not (accounts["a"] / "x").exists() and not (accounts["a"].parent / "x").exists()
        assert rows(app, "ext-skills") == ["alpha", "beta"]


async def test_e_opens_the_selected_file_in_the_editor_without_a_shell(accounts, monkeypatch):
    calls = []
    monkeypatch.setattr(extensions, "_run", lambda argv, **kw: calls.append((argv, kw)))
    monkeypatch.setenv("EDITOR", "myedit --wait")
    suspended = []

    @contextlib.contextmanager
    def fake_suspend(self):      # the headless driver cannot suspend
        suspended.append(1)
        yield
    monkeypatch.setattr(Host, "suspend", fake_suspend)
    app = Host()
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        app.query_one("#ext-skills").focus()
        await pilot.press("e")
        await settle(pilot)
        assert app.is_running and app._exception is None
    assert suspended == [1]
    argv, kw = calls[0]
    assert argv == ["myedit", "--wait", str(accounts["a"] / "skills/alpha/SKILL.md")]
    assert not kw.get("shell")


async def test_r_rescans_and_y_copies_to_another_account(accounts):
    app = Host()
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        make_skill(accounts["a"], "gamma")
        await pilot.press("r")
        await settle(pilot)
        assert "gamma" in rows(app, "ext-skills")
        app.query_one("#ext-skills").focus()
        await pilot.press("y")
        await settle(pilot)
        picker = app.screen.query_one(OptionList)
        assert picker.option_count == 1            # only b: same harness, not itself
        await pilot.press("enter")
        await settle(pilot)
        assert (accounts["b"] / "skills/alpha/SKILL.md").exists()


async def test_y_on_a_hook_copies_nothing(accounts):
    app = Host()
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        app.query_one("#ext-hooks").focus()
        await pilot.press("y")
        await settle(pilot)
        assert app.is_running and not list(accounts["b"].iterdir())


async def test_settings_mounts_the_section_and_warns_about_a_missing_skill(accounts, monkeypatch):
    from pl.tui import settings
    monkeypatch.setattr(C, "PROMPTS", {"spec": "/missing-skill {id}", "plan": "/alpha {id}"})
    monkeypatch.setattr(C, "STAGES", {})
    monkeypatch.setattr(C, "CONFIG_DIR", None)
    monkeypatch.setattr(settings.github, "_gh", lambda args: "Logged in\n")
    snap = {"rows": [], "prof": "", "parked": False, "at": "", "summary": "", "summary2": "", "needs": {}, "disp": "running"}
    m = {"specs_written": 0, "plans_written": 0, "approvals": 0, "errors_last": None}
    data = {"snapshot": snap, "metrics_by_window": {k: m for k in ("1h", "24h", "7d")},
            "daily": {"specs": [0] * 14, "plans": [0] * 14}, "pr_activity": {"opened": [], "merged": []}}
    app = PlApp(snapshot_provider=lambda: data, interval=3600)
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        app.action_tab("settings")
        await settle(pilot)
        assert app.query(extensions.ExtensionsView)
        text = " ".join(screen_text(app).split())
        assert "/missing-skill is not a skill or command of account a" in text
        warns = [str(w.render()) for w in app.query(".stage-warning") if w.display]
        assert len(warns) == 1 and "missing-skill" in warns[0]
