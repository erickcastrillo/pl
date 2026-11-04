import io
import json
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from textual.app import App
from textual.widgets import DataTable

from pl import config as C
from pl import skill_sources as S
from pl.tui import skills as tui_skills
from pl.tui.review import ConfirmScreen

OLD, NEW = "c" * 40, "d" * 40


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(C, "PROFILES", {})
    monkeypatch.setattr(C, "ACCOUNTS", {})
    monkeypatch.setattr(C, "SKILLS", {"library": str(tmp_path / "lib")}, raising=False)
    monkeypatch.setattr(C, "STATE_DIR", tmp_path / "state")
    C.STATE_DIR.mkdir()
    up = tmp_path / "up"
    (up / "skills" / "review").mkdir(parents=True)
    (up / "skills" / "review" / "SKILL.md").write_text("---\nname: review\ndescription: Review it\n---\nBody\n")
    (up / "skills" / "review" / "notes.md").write_text("notes\n")
    head = {"sha": OLD, "fail": False}

    def fake(argv, cwd=None, timeout=60):
        if argv[:2] == ["git", "ls-remote"]:
            if head["fail"]:
                raise RuntimeError("could not resolve host")
            return f"{head['sha']}\trefs/heads/main\n"
        if argv[:2] == ["git", "checkout"]:
            shutil.copytree(up, cwd, dirs_exist_ok=True, symlinks=True)
        return ""
    monkeypatch.setattr(S, "_git", fake)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    assert S.install("github:acme/kit/skills/review@main", False, io.StringIO()) == 0
    return {"up": up, "lib": tmp_path / "lib", "head": head}


def publish(env):
    """Upstream moves on: SKILL.md changes, notes.md goes, a script appears."""
    skill = env["up"] / "skills" / "review"
    (skill / "SKILL.md").write_text("---\nname: review\ndescription: Review it\n---\nBetter body\n")
    (skill / "notes.md").unlink()
    (skill / "run.sh").write_text("echo hi\n")
    env["head"]["sha"] = NEW


def tree(lib):
    return {p.relative_to(lib).as_posix(): p.read_bytes() for p in sorted(lib.rglob("*")) if p.is_file()}


def check(*a, **kw):
    out = io.StringIO()
    return S.check(out, *a, **kw), out.getvalue()


def update(name="review", yes=False):
    out = io.StringIO()
    return S.update(name, yes, out), out.getvalue()


def test_check_lists_a_newer_upstream_and_changes_nothing(env):
    publish(env)
    before = tree(env["lib"])
    code, out = check()
    assert code == 0
    assert f"review  acme/kit@{OLD[:7]} -> {NEW[:7]}" in out
    assert tree(env["lib"]) == before


def test_check_says_nothing_for_an_up_to_date_skill(env):
    code, out = check()
    assert code == 0 and "review" not in out


def test_check_json_and_network_failures(env):
    publish(env)
    code, out = check(as_json=True)
    row = json.loads(out)[0]
    assert code == 0 and row["name"] == "review" and row["update"] is True and row["latest"] == NEW
    env["head"]["fail"] = True
    code, out = check()
    assert code == 1 and "review" in out and "could not resolve host" in out


def test_update_shows_the_diff_and_asks(env, monkeypatch):
    publish(env)
    asked = []
    monkeypatch.setattr("builtins.input", lambda prompt="": asked.append(prompt) or "y")
    code, out = update()
    assert code == 0
    assert "-Body" in out and "+Better body" in out
    assert "run.sh" in out and "notes.md" in out
    assert out.index("script") < out.index("+Better body")
    assert asked == [f"Update review to acme/kit@{NEW[:7]}? [y/N]: "]
    assert "Better body" in (env["lib"] / "review" / "SKILL.md").read_text()
    assert not (env["lib"] / "review" / "notes.md").exists()
    rec = json.loads((env["lib"] / S.SOURCES_FILE).read_text())["review"]
    assert rec["sha"] == NEW and set(rec["files"]) == {"SKILL.md", "run.sh"}


def test_declining_changes_nothing(env, monkeypatch):
    publish(env)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    before = tree(env["lib"])
    code, out = update()
    assert code == 0 and "nothing updated" in out
    assert tree(env["lib"]) == before


def test_an_edited_install_is_kept_beside_the_new_one(env):
    publish(env)
    (env["lib"] / "review" / "SKILL.md").write_text("my own wording\n")
    code, out = update(yes=True)
    assert code == 0
    kept = list((env["lib"] / ".pl-edited").glob("review-*"))
    assert len(kept) == 1 and str(kept[0]) in out
    assert (kept[0] / "SKILL.md").read_text() == "my own wording\n"
    assert "Better body" in (env["lib"] / "review" / "SKILL.md").read_text()


def test_an_untouched_install_leaves_no_edited_copy(env):
    publish(env)
    assert update(yes=True)[0] == 0
    assert not (env["lib"] / ".pl-edited").exists()


def test_update_without_a_terminal_needs_yes(env, monkeypatch):
    publish(env)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    before = tree(env["lib"])
    code, _ = update()
    assert code == 2 and tree(env["lib"]) == before
    assert update(yes=True)[0] == 0


def test_update_of_an_unknown_skill_is_refused(env):
    assert update("nope", yes=True)[0] == 2


def test_nothing_calls_update_on_its_own():
    src = Path(__file__).parent.parent / "src" / "pl"
    callers = {p.name for p in src.rglob("*.py") if "skill_sources.update(" in p.read_text()}
    assert callers <= {"skills.py"}


def test_update_refuses_a_link_that_leaves_the_skill_folder_but_stays_in_the_checkout(env, capsys):
    publish(env)
    (env["up"] / ".ssh").mkdir()
    (env["up"] / ".ssh" / "id_rsa").write_text("harmless")
    (env["up"] / "skills" / "review" / "leak.md").symlink_to("../../.ssh/id_rsa")
    before = tree(env["lib"])
    code, _ = update(yes=True)
    assert code == 2
    assert "link out of the source" in capsys.readouterr().err
    assert tree(env["lib"]) == before


def test_update_replaces_a_skill_folder_that_has_become_a_link(env, tmp_path):
    publish(env)
    target = tmp_path / "elsewhere"
    shutil.move(env["lib"] / "review", target)
    (env["lib"] / "review").symlink_to(target)
    code, _ = update(yes=True)
    assert code == 0
    assert not (env["lib"] / "review").is_symlink()
    assert "Better body" in (env["lib"] / "review" / "SKILL.md").read_text()
    assert (target / "SKILL.md").read_text().endswith("Body\n")


def test_dry_run_prints_the_diff_and_changes_nothing(env, monkeypatch):
    publish(env)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("asked"))
    before, rec = tree(env["lib"]), S.records()["review"]["sha"]
    out = io.StringIO()
    assert S.update("review", False, out, dry_run=True) == 0
    assert "New or changed code" in out.getvalue() and "Better body" in out.getvalue()
    assert f"would update review to acme/kit@{NEW}" in out.getvalue()
    assert tree(env["lib"]) == before and S.records()["review"]["sha"] == rec


def test_update_with_the_shown_sha_applies_it(env):
    publish(env)
    assert S.update("review", True, io.StringIO(), sha=NEW) == 0
    assert S.records()["review"]["sha"] == NEW


def test_update_refuses_when_upstream_moved_after_the_diff(env, capsys):
    publish(env)
    before = tree(env["lib"])
    assert S.update("review", True, io.StringIO(), sha="e" * 40) == 1
    assert "upstream changed" in capsys.readouterr().err
    assert S.records()["review"]["sha"] == OLD and tree(env["lib"]) == before


def test_cli_update_accepts_dry_run_and_sha():
    from pl import cli
    a = cli.build_parser().parse_args(["skills", "update", "review", "--dry-run", "--sha", "abc"])
    assert a.dry_run is True and a.sha == "abc"


GITHUB = {"account": "library", "origin_label": f"GitHub acme/kit@{OLD[:7]}", "name": "review"}


def row(update=True, error=None):
    return {"name": "review", "update": update, "pinned": OLD, "latest": NEW, "error": error}


def test_update_label_and_block_rules():
    label, block = tui_skills.update_label, tui_skills.update_block
    assert label(GITHUB, {"review": row()}) == f"update available {OLD[:7]} -> {NEW[:7]}"
    assert label(GITHUB, {"review": row(False)}) == "up to date"
    assert label(GITHUB, {"review": row(False, "no network")}) == "check failed"
    assert label(GITHUB, {}) == "check failed"
    assert label(GITHUB, None) == "checking…"
    for origin in ("you", "built in", "plugin"):
        assert label({**GITHUB, "origin_label": origin}, {"review": row()}) == ""
    assert label({**GITHUB, "account": "work"}, {"review": row()}) == ""
    assert block(GITHUB, {"review": row()}) == ""
    assert block(None, {}) == "nothing selected"
    assert block({**GITHUB, "origin_label": "you"}, {}) == "not installed from GitHub"
    assert block(GITHUB, None) == "checking for updates…"
    assert block(GITHUB, {"review": row(False, "no network")}) == "check failed: no network"
    assert block(GITHUB, {"__error__": "boom"}) == "check failed: boom"
    assert block(GITHUB, {"review": row(False)}) == "up to date"


class Recorder:
    def __init__(self, checks, dry=None, gate=None, code=0, err=""):
        self.checks, self.gate, self.code, self.err = checks, gate, code, err
        self.dry = dry or f"New or changed code: script run.sh\n--- a/SKILL.md\n+Better body\nwould update review to acme/kit@{NEW}"
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        done = lambda out, code=0, err="": subprocess.CompletedProcess(argv, code, stdout=out, stderr=err)  # noqa: E731
        if "--check" in argv:
            if self.gate:
                self.gate.wait(5)
            return done(json.dumps(self.checks), self.code, self.err)
        if "--dry-run" in argv:
            return done(self.dry)
        return done(f"updated review to {NEW[:7]}")


class Host(App):
    def compose(self):
        yield tui_skills.SkillsView()


def review_cells(app):
    view = app.query_one(tui_skills.SkillsView)
    i = next(n for n, it in enumerate(view.rows) if it and it["name"] == "review")
    return view, i, [c.plain for c in app.query_one(DataTable).get_row_at(i)]


async def settle(app, pilot):
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


async def select_review(app, pilot):
    view, i, _ = review_cells(app)
    app.query_one(DataTable).move_cursor(row=i)
    await settle(app, pilot)


def note(app):
    return str(app.query_one("#skills-reasons").render())


def confirm_text(app):
    return app.screen.message if isinstance(app.screen, ConfirmScreen) else None


@pytest.mark.asyncio
async def test_rows_show_update_available_after_the_background_check(env, monkeypatch):
    rec = Recorder([row()])
    monkeypatch.setattr(tui_skills, "_run", rec)
    app = Host()
    async with app.run_test() as pilot:
        await settle(app, pilot)
        assert f"update available {OLD[:7]} -> {NEW[:7]}" in review_cells(app)[2]
    assert ["pl", "skills", "update", "--check", "--json"] in rec.calls


@pytest.mark.asyncio
async def test_a_failed_check_marks_the_row_and_shows_the_error_when_selected(env, monkeypatch):
    monkeypatch.setattr(tui_skills, "_run", Recorder([row(False, "could not resolve host")], code=1))
    app = Host()
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await select_review(app, pilot)
        assert "check failed" in review_cells(app)[2]
        assert "check failed: could not resolve host" in note(app)
        assert len(app.query_one(tui_skills.SkillsView).items) >= 1


@pytest.mark.asyncio
async def test_the_table_fills_before_the_check_returns(env, monkeypatch):
    gate = threading.Event()
    monkeypatch.setattr(tui_skills, "_run", Recorder([row()], gate=gate))
    app = Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        assert "checking…" in review_cells(app)[2]
        gate.set()
        await settle(app, pilot)
        assert f"update available {OLD[:7]} -> {NEW[:7]}" in review_cells(app)[2]


@pytest.mark.asyncio
async def test_u_shows_the_diff_and_no_changes_nothing(env, monkeypatch):
    rec = Recorder([row()])
    monkeypatch.setattr(tui_skills, "_run", rec)
    app = Host()
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await select_review(app, pilot)
        await pilot.press("u")
        await settle(app, pilot)
        text = confirm_text(app)
        assert "New or changed code" in text and f"Update review to acme/kit@{NEW[:7]}?" in text
        await pilot.press("n")
        await settle(app, pilot)
    assert not any("--yes" in c for c in rec.calls)


@pytest.mark.asyncio
async def test_u_yes_updates_only_the_shown_commit(env, monkeypatch):
    rec = Recorder([row()])
    monkeypatch.setattr(tui_skills, "_run", rec)
    app = Host()
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await select_review(app, pilot)
        await pilot.press("u")
        await settle(app, pilot)
        before = sum("--check" in c for c in rec.calls)
        await pilot.press("y")
        await settle(app, pilot)
        assert any(n.message == f"updated review to {NEW[:7]}" for n in app._notifications)
        assert sum("--check" in c for c in rec.calls) > before
    assert ["pl", "skills", "update", "review", "--yes", "--sha", NEW] in rec.calls


@pytest.mark.asyncio
async def test_u_on_an_up_to_date_row_says_why_and_runs_nothing(env, monkeypatch):
    rec = Recorder([row(False)])
    monkeypatch.setattr(tui_skills, "_run", rec)
    app = Host()
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await select_review(app, pilot)
        await pilot.press("u")
        await settle(app, pilot)
        assert any(n.message == "up to date" for n in app._notifications)
    assert not any("--dry-run" in c for c in rec.calls)
