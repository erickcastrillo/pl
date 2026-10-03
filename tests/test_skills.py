"""Skills section: every account's skills, agents and commands, viewed, edited, created, trashed, and shared through
pl's skills library. Temp HOME with fake accounts; nothing real is touched."""
import io
import os
import sys

import pytest
from rich.console import Console
from textual.app import App
from textual.widgets import DataTable, Input, Markdown, Select, TextArea

from pl import cli, skills
from pl import config as C
from pl.tui import skills as tui

SKILL = "---\nname: {n}\ndescription: {d}\n---\n\n# {n}\n\nBody of {n}.\n"


def make_skill(root, name, desc="Does a thing"):
    d = root / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(SKILL.format(n=name, d=desc))
    return d


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    a, b, c, g = (tmp_path / n for n in (".claude-a", ".claude-b", ".codex-c", ".agy-g"))
    for d in (a, b, c, g):
        d.mkdir()
    make_skill(a, "alpha", "Alpha skill")
    make_skill(a, "omc-helper", "From oh-my-claude")
    (a / "agents").mkdir()
    (a / "agents" / "reviewer.md").write_text("---\nname: reviewer\ndescription: Reviews code\n---\nBody\n")
    (a / "commands").mkdir()
    (a / "commands" / "deploy.md").write_text("---\ndescription: Deploy it\n---\nBody\n")
    (a / ".credentials.json").write_text('{"token": "do-not-read"}')
    plug = a / "plugins" / "cache" / "mkt" / "plug" / "1.0"
    make_skill(plug, "plugskill", "A plugin skill")
    make_skill(b, "beta", "Beta skill")
    (tmp_path / ".claude").mkdir()
    make_skill(tmp_path / ".claude", "team-wide", "Shared by everyone")
    monkeypatch.setattr(C, "PROFILES", {"a": a, "b": b, "c": c, "g": g})
    monkeypatch.setattr(C, "ACCOUNTS", {"a": {"harness": "claude"}, "b": {"harness": "claude"},
                                        "c": {"harness": "codex"}, "g": {"harness": "antigravity"}})
    monkeypatch.setattr(C, "SKILLS", {}, raising=False)
    return {"home": tmp_path, "a": a, "b": b, "c": c, "g": g}


def item(name, account=None):
    return next(i for i in skills.scan() if i["name"] == name and (account is None or i["account"] == account))


# ---------- listing ----------

def test_lists_skills_agents_and_commands_of_every_account_and_the_shared_folder(home):
    got = {(i["name"], i["account"], i["kind"]) for i in skills.scan()}
    assert {("alpha", "a", "skill"), ("beta", "b", "skill"), ("reviewer", "a", "agent"), ("deploy", "a", "command"),
            ("team-wide", "shared", "skill"), ("plugskill", "a", "skill")} <= got
    assert item("alpha")["description"] == "Alpha skill"
    assert item("omc-helper")["origin"] == "omc" and item("plugskill")["origin"] == "plugin"
    assert item("alpha")["origin"] == ""


def test_never_lists_credentials_or_non_md_files(home):
    (home["a"] / "skills" / "creds").mkdir()
    os.symlink(home["a"] / ".credentials.json", home["a"] / "skills" / "creds" / "SKILL.md")
    (home["a"] / "commands" / "notes.txt").write_text("x")
    names = {i["name"] for i in skills.scan()}
    assert "creds" not in names and "notes" not in names


def test_a_symlink_pointing_outside_the_account_is_not_followed(home, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    make_skill(outside, "evil", "Outside")
    (outside / "agent.md").write_text("outside agent")
    os.symlink(outside / "skills" / "evil", home["a"] / "skills" / "evil")
    os.symlink(outside / "agent.md", home["a"] / "agents" / "sneaky.md")
    names = {i["name"] for i in skills.scan()}
    assert "evil" not in names and "sneaky" not in names
    fake = {"name": "evil", "account": "a", "kind": "skill", "origin": "", "root": str(home["a"]),
            "path": str(home["a"] / "skills" / "evil" / "SKILL.md")}
    with pytest.raises(SystemExit, match="outside"):
        skills.read(fake)
    with pytest.raises(SystemExit, match="outside"):
        skills.save(fake, "new text")
    assert (outside / "skills" / "evil" / "SKILL.md").read_text().startswith("---")


# ---------- edit ----------

def test_save_backs_up_first_and_writes_the_new_text(home):
    it = item("alpha")
    old = skills.read(it)
    bak = skills.save(it, old + "More.\n")
    assert bak.name.startswith("SKILL.md.bak-") and bak.read_text() == old
    assert (home["a"] / "skills/alpha/SKILL.md").read_text() == old + "More.\n"
    assert "+More." in skills.diff(old, old + "More.\n")
    assert "alpha" in {i["name"] for i in skills.scan()} and not any(".bak" in i["name"] for i in skills.scan())


def test_plugin_files_are_refused_with_the_reason(home):
    it = item("plugskill")
    before = skills.read(it)
    with pytest.raises(SystemExit, match="plugin"):
        skills.save(it, "changed")
    with pytest.raises(SystemExit, match="plugin"):
        skills.trash(it)
    assert skills.read(it) == before


# ---------- create ----------

@pytest.mark.parametrize("bad", ["", "a", "Bad", "-x", "../x", "x/y", "a" * 50, "with space"])
def test_create_validates_the_name(home, bad):
    with pytest.raises(SystemExit, match="not a valid name"):
        skills.create("a", "skill", bad)


def test_create_writes_the_template_and_never_overwrites(home):
    p = skills.create("b", "skill", "fresh-one")
    assert p == home["b"] / "skills/fresh-one/SKILL.md"
    text = p.read_text()
    assert text.startswith("---\nname: fresh-one\ndescription: ") and "\n---\n" in text and "## " in text
    with pytest.raises(SystemExit, match="already exists"):
        skills.create("b", "skill", "fresh-one")
    a = skills.create("a", "agent", "helper")
    assert a == home["a"] / "agents/helper.md" and "name: helper" in a.read_text()
    with pytest.raises(SystemExit, match="already exists"):
        skills.create("a", "command", "deploy")
    assert "Deploy it" in (home["a"] / "commands/deploy.md").read_text()


# ---------- delete ----------

def test_delete_moves_the_folder_to_trash(home):
    skills.trash(item("alpha"))
    assert not (home["a"] / "skills/alpha").exists() and not (home["a"] / "skills/.trash").exists()
    moved = list((home["a"] / ".pl-trash/skills").iterdir())   # outside skills/: Claude does not read it
    assert len(moved) == 1 and moved[0].name.startswith("alpha-") and (moved[0] / "SKILL.md").is_file()
    assert "alpha" not in {i["name"] for i in skills.scan()}


# ---------- library: share and link ----------

def test_share_moves_the_skill_into_the_library_and_leaves_a_link(home):
    lib = skills.share("a", "alpha")
    assert lib == home["home"] / ".local/share/pl/skills/alpha" and (lib / "SKILL.md").is_file()
    src = home["a"] / "skills/alpha"
    assert src.is_symlink() and src.resolve() == lib.resolve()
    rows = {(i["name"], i["account"]): i for i in skills.scan()}
    assert ("alpha", "a") in rows and ("alpha", "library") in rows      # a link into the library is still listed
    assert rows[("alpha", "library")]["linked"] == ["a"]
    with pytest.raises(SystemExit, match="already"):
        skills.share("a", "alpha")


def test_share_refuses_a_name_already_in_the_library(home):
    skills.share("a", "alpha")
    make_skill(home["b"], "alpha", "Another alpha")
    with pytest.raises(SystemExit, match="already exists"):
        skills.share("b", "alpha")
    assert not (home["b"] / "skills/alpha").is_symlink()


def test_link_creates_a_link_for_claude_and_codex_and_never_overwrites(home):
    lib = skills.share("a", "alpha")
    for acct in ("b", "c", "g"):
        dest = skills.link("alpha", acct)
        assert dest == home[acct] / "skills/alpha" and dest.is_symlink() and dest.resolve() == lib.resolve()
    assert item("alpha", "library")["linked"] == ["a", "b", "c", "g"]
    make_skill(home["b"], "beta2")
    with pytest.raises(SystemExit, match="already exists"):
        skills.link("alpha", "b")


def test_link_refuses_unsupported_harnesses_clearly(home, monkeypatch):
    skills.share("a", "alpha")
    m = home["home"] / ".custom-m"
    m.mkdir()
    monkeypatch.setattr(C, "ACCOUNTS", {**C.ACCOUNTS, "m": {"harness": "custom"}})
    monkeypatch.setattr(C, "PROFILES", {**C.PROFILES, "m": m})
    monkeypatch.setattr(skills.harnesses, "account_harness", lambda a: skills.harnesses.Harness(name="custom", bin="custom"))
    with pytest.raises(SystemExit, match="not supported for custom"):
        skills.link("alpha", "m")
    assert not (m / "skills").exists()
    with pytest.raises(SystemExit, match="no library skill"):
        skills.link("nope", "b")


def test_install_in_all_harnesses_links_missing_skills(home):
    skills.share("a", "alpha")
    msgs = skills.install_in_all_harnesses("alpha")
    assert any("linked alpha into b" in m for m in msgs)
    assert any("linked alpha into c" in m for m in msgs)
    assert any("linked alpha into g" in m for m in msgs)
    for acct in ("b", "c", "g"):
        assert (home[acct] / "skills/alpha").is_symlink()


def test_link_refuses_a_skills_dir_that_leaves_the_account(home, tmp_path_factory):
    skills.share("a", "alpha")
    outside = tmp_path_factory.mktemp("elsewhere")
    os.symlink(outside, home["c"] / "skills")
    with pytest.raises(SystemExit, match="outside"):
        skills.link("alpha", "c")
    assert list(outside.iterdir()) == []


def test_library_folder_comes_from_config(home, monkeypatch):
    monkeypatch.setattr(C, "SKILLS", {"library": str(home["home"] / "lib")})
    assert skills.share("a", "alpha") == home["home"] / "lib/alpha"


def test_cli_list_share_and_link(home, monkeypatch, capsys):
    monkeypatch.setattr(C, "load", lambda *a, **k: None)
    monkeypatch.setattr(C, "CONFIG_DIR", home["home"])
    (home["home"] / "config.toml").write_text("")
    for argv in (["skills", "share", "alpha", "--account", "a"], ["skills", "link", "alpha", "b"], ["skills", "list"]):
        monkeypatch.setattr(sys, "argv", ["pl", *argv])
        cli.main()
    out = capsys.readouterr().out
    assert (home["b"] / "skills/alpha").is_symlink()
    line = next(ln for ln in out.splitlines() if ln.startswith("alpha") and "library" in ln and "a, b" in ln)
    assert line


# ---------- the screen ----------

def screen_text(app):
    console = Console(width=app.size.width, height=app.size.height, file=io.StringIO(), record=True,
                      force_terminal=True, color_system="truecolor", legacy_windows=False, safe_box=False)
    console.print(app.screen._compositor.render_update(full=True))
    return " ".join(console.export_text().split())


async def settle(pilot):
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()
    await pilot.pause()


class Host(App):
    def compose(self):
        yield tui.SkillsView()


def rows(app):
    t = app.query_one("#skills-table", DataTable)
    return [(str(t.get_row_at(i)[0]), str(t.get_row_at(i)[1])) for i in range(t.row_count)]


async def select(pilot, name, account):
    app = pilot.app
    t = app.query_one("#skills-table", DataTable)
    t.focus()
    t.move_cursor(row=rows(app).index((name, account)))
    await settle(pilot)


async def test_view_lists_two_accounts_and_renders_the_preview(home):
    app = Host()
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        got = rows(app)
        assert ("alpha", "a") in got and ("beta", "b") in got and ("team-wide", "shared") in got
        await select(pilot, "beta", "b")
        md = app.query_one("#skills-preview", Markdown)
        assert "Body of beta." in md.source
        assert "Body of beta." in screen_text(app)


async def test_e_edits_in_app_and_saves_with_a_backup_after_confirming(home):
    app = Host()
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        await select(pilot, "alpha", "a")
        await pilot.press("e")
        await settle(pilot)
        ta = app.screen.query_one(TextArea)
        ta.load_text(ta.text + "Added line.\n")
        await pilot.press("ctrl+s")
        await settle(pilot)
        assert "+Added line." in screen_text(app)                  # the diff is shown before anything is written
        assert "Added line." not in (home["a"] / "skills/alpha/SKILL.md").read_text()
        await pilot.press("y")
        await settle(pilot)
        assert (home["a"] / "skills/alpha/SKILL.md").read_text().endswith("Added line.\n")
        assert len(list((home["a"] / "skills/alpha").glob("SKILL.md.bak-*"))) == 1


async def test_e_on_a_plugin_skill_is_refused(home):
    app = Host()
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        await select(pilot, "plugskill", "a")
        await pilot.press("e")
        await settle(pilot)
        assert not app.screen.query(TextArea)
        assert "plugin" in screen_text(app)


async def test_n_creates_from_the_template_and_opens_the_editor(home):
    app = Host()
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        app.query_one("#skills-table").focus()
        await pilot.press("n")
        await settle(pilot)
        app.screen.query_one("#skills-new-account", Select).value = "b"
        app.screen.query_one("#skills-new-name", Input).value = "Bad Name"
        app.screen.query_one("#skills-new-name", Input).focus()
        await pilot.press("enter")
        await settle(pilot)
        assert not (home["b"] / "skills/Bad Name").exists() and app.is_running
        await pilot.press("n")
        await settle(pilot)
        app.screen.query_one("#skills-new-account", Select).value = "b"
        app.screen.query_one("#skills-new-name", Input).value = "made-here"
        app.screen.query_one("#skills-new-name", Input).focus()
        await pilot.press("enter")
        await settle(pilot)
        assert (home["b"] / "skills/made-here/SKILL.md").read_text().startswith("---\nname: made-here\n")
        assert "name: made-here" in app.screen.query_one(TextArea).text


async def test_d_asks_then_moves_to_trash(home):
    app = Host()
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        await select(pilot, "beta", "b")
        await pilot.press("d")
        await settle(pilot)
        assert (home["b"] / "skills/beta").is_dir()                 # nothing moves before the yes
        await pilot.press("y")
        await settle(pilot)
        assert not (home["b"] / "skills/beta").exists() and (home["b"] / ".pl-trash/skills").is_dir()
        assert ("beta", "b") not in rows(app)


async def test_palette_entry_opens_the_skills_section(home):
    from pl.tui.app import PlApp
    from test_tui_views import Provider
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        cmd = next(c for c in app.get_system_commands(app.screen) if c.title == "Skills: view, edit, create")
        cmd.callback()
        await settle(pilot)
        assert app.active_tab == "settings" and app.focused is app.query_one("#skills-table")


# ---------- launches: Claude loads the library as a plugin; other harnesses get the skill inlined ----------

@pytest.fixture
def lib(home, monkeypatch):
    monkeypatch.setattr(C, "STATE_DIR", home["home"] / "state")
    skills.share("a", "alpha")
    return home


def test_claude_launch_carries_plugin_dir_with_a_valid_manifest(lib):
    import json
    import shlex

    from pl import harnesses
    script = harnesses.launch_script(harnesses.get("claude"), "b", "/alpha go", "sid-1", "lbl")
    argv = shlex.split(script.splitlines()[-1])[1:]
    plugin = C.STATE_DIR / "plugin"                       # per profile, never shared across profiles
    assert argv[:3] == ["claude", "--plugin-dir", str(plugin)]
    assert not (lib["home"] / ".local/share/pl/plugin").exists()
    assert json.loads((plugin / ".claude-plugin/plugin.json").read_text())["name"] == "pl"
    assert (plugin / "skills/alpha/SKILL.md").is_file()
    assert argv[-1] == "/pl:alpha go"                     # plugin skills are namespaced; b has no alpha of its own
    a_argv = shlex.split(harnesses.launch_script(harnesses.get("claude"), "a", "/alpha go", "s", "l").splitlines()[-1])
    assert a_argv[-1] == "/alpha go"                      # a links alpha itself: unchanged
    h_argv, _ = harnesses.headless_argv(harnesses.get("claude"), "b", "hi")
    assert h_argv[:2] == ["claude", "--plugin-dir"]


def test_no_library_means_no_plugin_dir(home, monkeypatch):
    import shlex

    from pl import harnesses
    monkeypatch.setattr(C, "STATE_DIR", home["home"] / "state")
    argv = shlex.split(harnesses.launch_script(harnesses.get("claude"), "a", "/alpha go", "s", "l").splitlines()[-1])
    assert "--plugin-dir" not in argv and argv[-1] == "/alpha go"


def test_codex_launch_inlines_the_library_skill(lib):
    import shlex

    from pl import harnesses
    argv = shlex.split(harnesses.launch_script(harnesses.get("codex"), "c", "/alpha card-42", None, "l").splitlines()[-1])
    prompt = argv[-1]
    assert prompt.startswith("Follow the instructions in ") and "--plugin-dir" not in argv
    f = prompt.removeprefix("Follow the instructions in ").rstrip(".")
    text = open(f).read()
    assert "Body of alpha." in text and "card-42" in text
    assert oct(os.stat(f).st_mode & 0o777) == "0o600"


def test_a_missing_skill_name_keeps_the_prompt(lib):
    import shlex

    from pl import harnesses
    for h, acct in (("codex", "c"), ("claude", "b")):
        argv = shlex.split(harnesses.launch_script(harnesses.get(h), acct, "/nope X", None, "l").splitlines()[-1])
        assert argv[-1] == "/nope X"


# ---------- round 1 fixes ----------

def notes(app):
    """Every toast's text: a long path wraps a toast on screen."""
    return " ".join(n.message for n in app._notifications)


def test_the_backup_is_a_byte_for_byte_copy(home):
    f = home["a"] / "skills/alpha/SKILL.md"
    raw = b"---\r\nname: alpha\r\ndescription: Alpha skill\r\n---\r\n\r\nBody.\r\n"
    f.write_bytes(raw)
    bak = skills.save(item("alpha"), "new\n")
    assert bak.read_bytes() == raw and f.read_bytes() == b"new\n"


@pytest.mark.parametrize("raw, why", [(b"---\nname: alpha\n---\n" + b"x" * (1 << 20), "larger than 1 MiB"),
                                      (b"---\nname: alpha\n---\n\xff\xfe broken \xc3\n", "not UTF-8")], ids=["big", "binary"])
def test_a_big_or_non_utf8_file_is_not_opened_in_the_editor_and_never_damaged(home, raw, why):
    f = home["a"] / "skills/alpha/SKILL.md"
    f.write_bytes(raw)
    it = item("alpha")
    with pytest.raises(SystemExit, match=why):
        skills.edit_text(it)
    with pytest.raises(SystemExit, match=why):
        skills.save(it, "short\n")
    assert f.read_bytes() == raw and not list(f.parent.glob("SKILL.md.bak-*"))


async def test_e_on_a_big_file_says_why_and_opens_nothing(home):
    (home["a"] / "skills/alpha/SKILL.md").write_bytes(b"x" * ((1 << 20) + 1))
    app = Host()
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        await select(pilot, "alpha", "a")
        await pilot.press("e")
        await settle(pilot)
        assert not app.screen.query(TextArea) and "larger than 1 MiB" in notes(app)


def test_save_refuses_when_the_file_changed_on_disk_since_it_was_opened(home):
    it = item("alpha")
    opened = skills.edit_text(it)
    (home["a"] / "skills/alpha/SKILL.md").write_text("someone else's edit\n")
    with pytest.raises(SystemExit, match="changed on disk; reopen"):
        skills.save(it, opened + "mine\n", expect=opened)
    assert (home["a"] / "skills/alpha/SKILL.md").read_text() == "someone else's edit\n"


def test_save_never_writes_through_a_link_swapped_in_after_the_check(home, tmp_path_factory, monkeypatch):
    outside = tmp_path_factory.mktemp("outside") / "target.md"
    outside.write_text("untouched\n")
    f = home["a"] / "skills/alpha/SKILL.md"
    real = skills._stamp

    def swap(p):
        if not f.is_symlink():
            f.unlink()
            os.symlink(outside, f)
        return real(p)
    monkeypatch.setattr(skills, "_stamp", swap)
    with pytest.raises(SystemExit):
        skills.save(item("alpha"), "through the link\n")
    assert outside.read_text() == "untouched\n"


async def test_esc_with_unsaved_edits_asks_before_discarding(home):
    app = Host()
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        await select(pilot, "alpha", "a")
        await pilot.press("e")
        await settle(pilot)
        ta = app.screen.query_one(TextArea)
        ta.load_text(ta.text + "Unsaved.\n")
        await pilot.press("escape")
        await settle(pilot)
        assert "Discard" in screen_text(app)
        await pilot.press("n")
        await settle(pilot)
        assert app.screen.query(TextArea) and "Unsaved." in app.screen.query_one(TextArea).text
        await pilot.press("escape")
        await settle(pilot)
        await pilot.press("y")
        await settle(pilot)
        assert not app.screen.query(TextArea)
        assert "Unsaved." not in (home["a"] / "skills/alpha/SKILL.md").read_text()


async def test_ctrl_s_after_a_change_on_disk_says_reopen(home):
    app = Host()
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        await select(pilot, "alpha", "a")
        await pilot.press("e")
        await settle(pilot)
        (home["a"] / "skills/alpha/SKILL.md").write_text("theirs\n")
        ta = app.screen.query_one(TextArea)
        ta.load_text(ta.text + "mine\n")
        await pilot.press("ctrl+s")
        await settle(pilot)
        await pilot.press("y")
        await settle(pilot)
        assert "changed on disk; reopen" in notes(app)
        assert (home["a"] / "skills/alpha/SKILL.md").read_text() == "theirs\n"


def test_trash_puts_agents_under_pl_trash_and_refuses_a_linked_trash_folder(home, tmp_path_factory):
    dest = skills.trash(item("reviewer"))
    assert dest.parent == home["a"] / ".pl-trash/agents" and dest.name.startswith("reviewer-") and dest.suffix == ".md"
    assert not (home["a"] / "agents/.trash").exists()
    outside = tmp_path_factory.mktemp("away")
    os.symlink(outside, home["b"] / ".pl-trash")
    with pytest.raises(SystemExit, match="refused"):
        skills.trash(item("beta"))
    assert (home["b"] / "skills/beta/SKILL.md").is_file() and list(outside.iterdir()) == []


def test_trash_refuses_a_library_skill_that_accounts_still_link(home):
    skills.share("a", "alpha")
    with pytest.raises(SystemExit, match="still link"):
        skills.trash(item("alpha", "library"))
    assert (skills.library() / "alpha/SKILL.md").is_file()


async def test_d_warns_that_links_and_pl_prompts_will_break(home):
    app = Host()
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        await select(pilot, "beta", "b")
        await pilot.press("d")
        await settle(pilot)
        text = screen_text(app)
        assert "other profiles" in text and "/pl:beta" in text


@pytest.mark.parametrize("bad, why", [("relative/lib", "absolute"), ("HOME", "home"), ("ACCOUNT", "account")])
def test_the_library_must_be_absolute_not_home_and_not_cover_an_account(home, monkeypatch, bad, why):
    value = {"HOME": str(home["home"]), "ACCOUNT": str(home["a"])}.get(bad, bad)
    monkeypatch.setattr(C, "SKILLS", {"library": value})
    with pytest.raises(SystemExit, match=f"config.*{why}"):
        skills.library()


def test_share_refuses_a_folder_holding_a_credential_or_a_link_out(home, tmp_path_factory):
    (home["a"] / "skills/alpha/.credentials.json").write_text("{}")
    with pytest.raises(SystemExit, match="credential"):
        skills.share("a", "alpha")
    assert not (home["a"] / "skills/alpha").is_symlink()
    out = tmp_path_factory.mktemp("o")
    os.symlink(out, home["b"] / "skills/beta/escape")
    with pytest.raises(SystemExit, match="link out"):
        skills.share("b", "beta")
    assert not (home["b"] / "skills/beta").is_symlink()


@pytest.mark.parametrize("link_name, target", [("innocent.md", ".credentials.md"), (".credentials.md", "reviewer.md")])
def test_a_credential_name_on_the_link_or_its_target_is_never_read(home, link_name, target):
    if target == ".credentials.md":
        (home["a"] / "agents" / target).write_text("secret")
    p = home["a"] / "agents" / link_name
    os.symlink(home["a"] / "agents" / target, p)
    fake = {"name": "x", "account": "a", "kind": "agent", "origin": "", "root": str(home["a"]), "path": str(p)}
    with pytest.raises(SystemExit, match="refused"):
        skills.read(fake)


def test_no_plugin_dir_means_no_pl_prefix(lib):
    import shlex

    from pl import harnesses
    (C.STATE_DIR / "plugin").parent.mkdir(parents=True, exist_ok=True)
    (C.STATE_DIR / "plugin").write_text("a file in the way")       # the plugin folder cannot be made
    argv = shlex.split(harnesses.launch_script(harnesses.get("claude"), "b", "/alpha go", "s", "l").splitlines()[-1])
    assert "--plugin-dir" not in argv and argv[-1] == "/alpha go"
    argv = shlex.split(harnesses.launch_script(harnesses.get("claude"), "b", "/alpha go", "s", "l",
                                               plugin=False).splitlines()[-1])
    assert "--plugin-dir" not in argv and argv[-1] == "/alpha go"


def test_a_work_dir_skill_of_the_same_name_keeps_the_prompt(lib, monkeypatch):
    import shlex

    from pl import harnesses
    wd = lib["home"] / "code"
    make_skill(wd / ".claude", "alpha", "the repo's own alpha")
    monkeypatch.setattr(C, "WORK_DIR", wd)
    argv = shlex.split(harnesses.launch_script(harnesses.get("claude"), "b", "/alpha go", "s", "l").splitlines()[-1])
    assert argv[-1] == "/alpha go"


def test_codex_keeps_its_own_skill_of_the_same_name(lib):
    import shlex

    from pl import harnesses
    make_skill(lib["c"], "alpha", "Codex's own alpha")
    argv = shlex.split(harnesses.launch_script(harnesses.get("codex"), "c", "/alpha x", None, "l").splitlines()[-1])
    assert argv[-1] == "/alpha x"


def test_inlined_prompt_files_are_0600_and_old_ones_are_removed(lib):
    import hashlib
    import shlex
    import time

    from pl import harnesses
    d = C.STATE_DIR / "launch"
    d.mkdir(parents=True, exist_ok=True)
    old, fresh, script = d / "skill-old-123.md", d / "skill-fresh-456.md", d / "launch-abc.sh"
    for f in (old, fresh, script):
        f.write_text("x")
    two_days = time.time() - 2 * 86400
    os.utime(old, (two_days, two_days))
    os.utime(script, (two_days, two_days))
    same = d / f"skill-alpha-{hashlib.sha1(b'/alpha y').hexdigest()[:12]}.md"
    same.write_text("x")
    os.chmod(same, 0o644)
    argv = shlex.split(harnesses.launch_script(harnesses.get("codex"), "c", "/alpha y", None, "l").splitlines()[-1])
    f = argv[-1].removeprefix("Follow the instructions in ").rstrip(".")
    assert f == str(same) and oct(os.stat(f).st_mode & 0o777) == "0o600"
    assert not old.exists() and fresh.exists() and script.exists()


async def test_s_is_left_to_standup_and_S_shares(home):
    app = Host()
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        await select(pilot, "beta", "b")
        await pilot.press("s")
        await settle(pilot)
        assert "skills library" not in screen_text(app)
        await pilot.press("S")
        await settle(pilot)
        assert "skills library" in screen_text(app)


async def test_the_preview_is_read_off_the_ui_thread(home, monkeypatch):
    import threading
    seen = []
    real = skills.read

    def spy(it):
        seen.append(threading.current_thread() is threading.main_thread())
        return real(it)
    monkeypatch.setattr(skills, "read", spy)
    app = Host()
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        await select(pilot, "beta", "b")
        assert seen and not any(seen)
        assert "Body of beta." in app.query_one("#skills-preview", Markdown).source
