"""WP11: Settings saves config.toml safely, Loops shows the live screen, a GitHub Project can be created."""
import argparse
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import tomlkit
from rich.console import Console
from textual.widgets import Input

from pl import agents, dispatch, harnesses, profiles
from pl import config as C
from pl.trackers import github
from pl.tui import settings
from pl.tui.app import PlApp

CONFIG = """# my work profile
tmux_session = "pl-work"

[accounts.main]
harness = "claude"
config_dir = "{acct}"

[tracker]
type = "acme-rest"
board_id = "board-1"
token = "${{TOKEN}}"   # read from the environment

[dispatch]
max_runs = 3

[custom]
keep_me = "yes"
"""


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("PL_CONFIG_DIR", "PL_TMUX_SESSION"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(github, "_gh", lambda args: pytest.fail(f"unexpected gh call {args}"))
    C.load()
    yield tmp_path
    harnesses._CACHE.clear()
    for k, v in saved.items():
        setattr(C, k, v)


@pytest.fixture
def work(fake_home):
    acct = fake_home / ".claude-main"
    acct.mkdir()
    d = fake_home / ".pl-work"
    d.mkdir()
    (d / "config.toml").write_text(CONFIG.format(acct=acct))
    C.load("work")
    return d / "config.toml"


def doc_of(p):
    return tomlkit.parse(p.read_text())


# ---------- config.save ----------

def test_save_round_trip_keeps_comments_unknown_keys_and_secret_refs(work):
    doc = doc_of(work)
    doc["dispatch"]["max_runs"] = 5
    C.save(doc)
    text = work.read_text()
    assert "# my work profile" in text and "# read from the environment" in text
    assert 'keep_me = "yes"' in text
    assert 'token = "${TOKEN}"' in text          # never expanded
    assert "max_runs = 5" in text
    assert oct(work.stat().st_mode & 0o777) == "0o600"
    assert [p.name for p in work.parent.iterdir() if p.name != "state"] == ["config.toml"]
    # acceptance: a new process sees the saved value
    out = subprocess.run([sys.executable, "-c", "from pl import config as C; C.load('work'); print(C.DISPATCH['max_runs'])"],
                         capture_output=True, text=True, env={**os.environ, "HOME": str(work.parent.parent)}, timeout=60)
    assert out.stdout.strip() == "5", out.stderr


def test_crash_between_write_and_replace_keeps_the_old_file(work, monkeypatch):
    before = work.read_bytes()
    doc = doc_of(work)
    doc["dispatch"]["max_runs"] = 9

    def boom(*a):
        raise OSError("disk gone")
    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        C.save(doc)
    assert work.read_bytes() == before
    assert [p.name for p in work.parent.iterdir() if p.name != "state"] == ["config.toml"]


@pytest.mark.parametrize("change, words", [
    (lambda d: d["accounts"].add("Bad Name", {"harness": "claude", "config_dir": "/tmp"}), "Bad Name"),
    (lambda d: d["accounts"]["main"].__setitem__("config_dir", "~/.claude-nowhere"), "does not exist"),
    (lambda d: d["accounts"]["main"].__setitem__("harness", "nope"), "claude"),
    (lambda d: d["tracker"].__setitem__("board_id", ""), "board_id"),
])
def test_validation_refuses_and_leaves_the_file_byte_identical(work, change, words):
    before = work.read_bytes()
    doc = doc_of(work)
    change(doc)
    with pytest.raises(SystemExit) as e:
        C.save(doc)
    assert words in str(e.value)
    assert work.read_bytes() == before


@pytest.mark.parametrize("key, value", [("interval", -1), ("interval", 0), ("max_runs", True), ("max_prep", -1), ("max_runs", "3")])
def test_validation_refuses_bad_dispatch_numbers(work, key, value):
    before = work.read_bytes()
    doc = doc_of(work)
    doc["dispatch"][key] = value
    with pytest.raises(SystemExit) as e:
        C.save(doc)
    assert f"dispatch.{key}" in str(e.value) and work.read_bytes() == before


def test_validation_refuses_a_file_that_load_would_reject(work):
    before = work.read_bytes()
    doc = doc_of(work)
    doc["gates"] = 1
    with pytest.raises(SystemExit) as e:
        C.save(doc)
    assert "would not load" in str(e.value) and work.read_bytes() == before


def test_saving_clears_the_harness_cache(work):
    harnesses._CACHE[work] = {"stale": {}}
    C.save(doc_of(work))
    assert work not in harnesses._CACHE


# ---------- the Settings and Loops screens ----------

def app_data(rows):
    snap = {"rows": rows, "prof": "", "parked": False, "at": "", "summary": "", "summary2": "", "needs": {}, "disp": "running"}
    m = {"specs_written": 0, "plans_written": 0, "approvals": 0, "errors_last": None}
    return {"snapshot": snap, "metrics_by_window": {k: m for k in ("1h", "24h", "7d")},
            "daily": {"specs": [0] * 14, "plans": [0] * 14}, "pr_activity": {"opened": [], "merged": []}}


def screen_text(app):
    console = Console(width=app.size.width, height=app.size.height, file=io.StringIO(), record=True,
                      force_terminal=True, color_system="truecolor", legacy_windows=False, safe_box=False)
    console.print(app.screen._compositor.render_update(full=True))
    return console.export_text()


async def settle(pilot):
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()
    await pilot.pause()


@pytest.fixture
def gh_signed_in(monkeypatch):
    monkeypatch.setattr(github, "_gh", lambda args: "github.com\n  ✓ Logged in to github.com account octo (keyring)\n")


async def test_no_profile_settings_are_read_only(gh_signed_in):
    app = PlApp(snapshot_provider=lambda: app_data([]), interval=3600)
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        app.action_tab("settings")
        await settle(pilot)
        assert "no profile loaded: settings are read-only" in screen_text(app)
        inputs = list(app.query(Input))
        assert inputs and all(i.disabled for i in inputs)
    with pytest.raises(SystemExit) as e:
        C.save(tomlkit.document())
    assert "pl profiles new <name>" in str(e.value)


async def test_connections_shows_the_subscription_line(work, gh_signed_in):
    C.GH_CONFIG_DIR = Path("/g/gh-work")             # the path is shown; nothing inside it is read (it need not exist)
    app = PlApp(snapshot_provider=lambda: app_data([]), interval=3600)
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        app.action_tab("settings")
        await settle(pilot)
        text = " ".join(screen_text(app).split())
        assert settings.SUBSCRIPTION_LINE in text
        assert "Logged in to github.com account octo" in text
        assert "(gh folder /g/gh-work)" in text



async def test_settings_screen_saves_only_the_edited_field(work, gh_signed_in):
    app = PlApp(snapshot_provider=lambda: app_data([]), interval=3600)
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        app.action_tab("settings")
        await settle(pilot)
        view = app.query_one(settings.SettingsView)
        wid = next(w for w, (p, _, _) in view.fields.items() if p == ("dispatch", "max_runs"))
        app.query_one(f"#{wid}", Input).value = "6"
        view.action_save()
        await settle(pilot)
    after = doc_of(work)
    assert after["dispatch"]["max_runs"] == 6 and C.DISPATCH["max_runs"] == 6
    assert "loops" not in after and "stages" not in after and 'token = "${TOKEN}"' in work.read_text()

async def test_loops_tab_shows_the_selected_loops_screen(monkeypatch):
    monkeypatch.setattr(C, "SERVICES", {"merge-check": {"prompt": "/loop 30m /merge-check", "profile": "acme"},
                                        "call-ingest": {"prompt": "/loop 15m /call-ingest", "profile": "acme"}})
    rows = [{"kind": "loop_idle", "card": None, "loop": "merge-check", "col": "Loops", "worker": {"pane": "%1", "window": "@1"},
             "win": "merge-check", "text": " ○ merge-check     on       every 30m   active 4m ago"},
            {"kind": "loop_busy", "card": None, "loop": "call-ingest", "col": "Loops", "worker": {"pane": "%2", "window": "@2"},
             "win": "call-ingest", "text": " ● call-ingest     working  every 15m   active 1m ago"}]
    monkeypatch.setattr(agents, "pane_tail", lambda pane, n: [f"SCREEN OF {pane} ({n} lines)"])
    app = PlApp(snapshot_provider=lambda: app_data(rows), interval=3600)
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        app.action_tab("loops")
        await settle(pilot)
        text = screen_text(app)
        assert "SCREEN OF %1 (40 lines)" in text and "/loop 30m /merge-check" in text
        await pilot.press("down")
        await settle(pilot)
        assert "SCREEN OF %2 (40 lines)" in screen_text(app)


async def test_pressing_f_through_every_filter_survives_a_dict_valued_field(work, monkeypatch):
    (C.STATE_DIR).mkdir(parents=True, exist_ok=True)
    (C.STATE_DIR / "events.jsonl").write_text(json.dumps({"kind": "moved", "to": {"x": 1}}) + "\n"
                                              + json.dumps({"kind": "moved", "to": ["a"], "loop": {"y": 1}}) + "\n")
    app = PlApp(snapshot_provider=lambda: app_data([]), interval=3600)
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        app.action_tab("activity")
        await settle(pilot)
        for _ in range(6):
            await pilot.press("f")
            await settle(pilot)
        assert app.is_running


# ---------- copying an Activity row ----------

LONG = "boom " + "x" * 200 + " END"


async def open_activity(work, monkeypatch, events, which):
    from pl.tui import loops
    C.STATE_DIR.mkdir(parents=True, exist_ok=True)
    (C.STATE_DIR / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    copied, piped = [], []
    monkeypatch.setattr(PlApp, "copy_to_clipboard", lambda self, text: copied.append(text))
    monkeypatch.setattr(loops.shutil, "which", lambda name: f"/bin/{name}" if name in which else None)
    monkeypatch.setattr(loops, "_copy_run", lambda argv, text: piped.append((list(argv), text)))
    return copied, piped


EVENT = {"ts": "2026-09-29T10:11:12+00:00", "kind": "error", "card": "abcdef1234567890", "message": LONG, "stage": "spec"}
WANT = f"10:11:12  error  abcdef1234567890  {LONG}\nstage=spec"


async def test_y_copies_the_full_untruncated_event(work, monkeypatch):
    copied, piped = await open_activity(work, monkeypatch, [EVENT], {"pbcopy", "xclip"})
    app = PlApp(snapshot_provider=lambda: app_data([]), interval=3600)
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        app.action_tab("activity")
        await settle(pilot)
        await pilot.press("y")
        await settle(pilot)
        assert copied == [WANT]
        assert piped == [(["pbcopy"], WANT)]      # the first copier on PATH only


async def test_y_with_no_copier_on_path_still_uses_the_terminal(work, monkeypatch):
    copied, piped = await open_activity(work, monkeypatch, [EVENT], set())
    app = PlApp(snapshot_provider=lambda: app_data([]), interval=3600)
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        app.action_tab("activity")
        await settle(pilot)
        await pilot.press("y")
        await settle(pilot)
        assert copied == [WANT] and piped == []


async def test_y_with_no_event_says_to_select_one(work, monkeypatch):
    copied, piped = await open_activity(work, monkeypatch, [], {"pbcopy"})
    notes = []
    app = PlApp(snapshot_provider=lambda: app_data([]), interval=3600)
    monkeypatch.setattr(app, "notify", lambda msg, **kw: notes.append((msg, kw)))
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        app.action_tab("activity")
        await settle(pilot)
        await pilot.press("y")
        await settle(pilot)
        assert copied == [] and piped == []
        assert ("select an event first", {}) in [(m, {}) for m, _ in notes]


async def test_y_notifies_the_first_60_characters_as_plain_text(work, monkeypatch):
    copied, piped = await open_activity(work, monkeypatch, [EVENT], set())
    notes = []
    app = PlApp(snapshot_provider=lambda: app_data([]), interval=3600)
    monkeypatch.setattr(app, "notify", lambda msg, **kw: notes.append((msg, kw)))
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        app.action_tab("activity")
        await settle(pilot)
        await pilot.press("y")
        await settle(pilot)
        assert (f"copied: {WANT[:60]}", False) in [(m, kw.get("markup")) for m, kw in notes]


def test_copy_run_pipes_text_on_stdin_with_a_5s_timeout_and_ignores_errors(monkeypatch):
    from pl.tui import loops
    seen = []
    monkeypatch.setattr(loops.subprocess, "run", lambda argv, **kw: seen.append((argv, kw)))
    loops._copy_run(["pbcopy"], "hi")
    assert seen[0][0] == ["pbcopy"] and seen[0][1]["input"] == "hi" and seen[0][1]["timeout"] == 5
    monkeypatch.setattr(loops.subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(OSError("gone")))
    loops._copy_run(["pbcopy"], "hi")       # no raise


# ---------- creating a GitHub Project ----------

CREATED = {"number": 7, "url": "https://github.com/users/octo/projects/7", "id": "PVT_x"}


def fake_gh(calls, fail_field=False):
    def gh(args):
        calls.append(list(args))
        if args[:2] == ["project", "create"]:
            return json.dumps(CREATED)
        if args[:2] == ["project", "field-create"]:
            if fail_field:
                raise SystemExit("pl: gh project field-create: boom")
            return "{}"
        pytest.fail(f"unexpected gh call {args}")
    return gh


def expected_argv(title):
    return [["project", "create", "--owner", "octo", "--title", title, "--format", "json"],
            ["project", "field-create", "7", "--owner", "octo", "--name", "pl stage", "--data-type", "SINGLE_SELECT",
             "--single-select-options", ",".join(C.COLUMNS)]]


def test_profiles_new_can_create_a_github_project(fake_home, monkeypatch):
    calls = []
    monkeypatch.setattr(github, "_gh", fake_gh(calls))
    profiles.cmd_new(["gh1", "--github-project", "create", "--owner", "octo", "--title", "My\x07 pl"])
    assert calls == expected_argv("My pl")
    t = tomlkit.parse((fake_home / ".pl-gh1" / "config.toml").read_text())
    assert t["tracker"]["type"] == "github-project" and t["tracker"]["owner"] == "octo"
    assert t["tracker"]["number"] == 7 and t["tracker"]["status_field"] == "pl stage"


def test_settings_create_project_saves_number_and_status_field(work, monkeypatch):
    doc = doc_of(work)
    doc["tracker"] = {"type": "github-project", "owner": "octo"}
    C.save(doc)
    C.load("work")
    calls = []
    monkeypatch.setattr(github, "_gh", fake_gh(calls))
    settings.create_project("octo", "pl work")
    assert calls == expected_argv("pl work")
    t = doc_of(work)
    assert t["tracker"]["number"] == 7 and t["tracker"]["status_field"] == "pl stage"
    assert 'keep_me = "yes"' in work.read_text()


@pytest.mark.parametrize("owner", ["-octo", "oc to", "o/x", "", "a" * 40])
def test_bad_owner_is_refused_before_any_gh_call(fake_home, monkeypatch, owner):
    calls = []
    monkeypatch.setattr(github, "_gh", fake_gh(calls))
    with pytest.raises(SystemExit):
        github.GitHubProject.create_project(owner, "t", C.COLUMNS)
    with pytest.raises(SystemExit):
        profiles.cmd_new(["gh2", "--github-project", "create", "--owner", owner])
    assert calls == []
    assert not (fake_home / ".pl-gh2").exists()


def test_field_create_failure_reports_the_project_url_and_saves_nothing(work, fake_home, monkeypatch):
    doc = doc_of(work)
    doc["tracker"] = {"type": "github-project", "owner": "octo"}
    C.save(doc)
    C.load("work")
    before = work.read_bytes()
    monkeypatch.setattr(github, "_gh", fake_gh([], fail_field=True))
    with pytest.raises(SystemExit) as e:
        settings.create_project("octo", "pl work")
    assert CREATED["url"] in str(e.value)
    assert work.read_bytes() == before
    with pytest.raises(SystemExit) as e:
        profiles.cmd_new(["gh3", "--github-project", "create", "--owner", "octo"])
    assert CREATED["url"] in str(e.value)
    assert not (fake_home / ".pl-gh3").exists()


# ---------- the dispatcher picks up saved settings ----------

def run_dispatcher(work, monkeypatch, edit, **flags):
    """Run two passes; `edit` rewrites the file after pass 1. Returns what each pass received."""
    got = []

    def once(max_runs, dry, max_prep=2, pull=True):
        got.append((max_runs, max_prep, C.TMUX_SESSION, dict(harnesses._CACHE)))
        if len(got) == 1:
            harnesses._CACHE[work] = {"stale": {}}
            work.write_text(edit(work.read_text()))
        else:
            raise KeyboardInterrupt
    monkeypatch.setattr(dispatch, "dispatch_once", once)
    monkeypatch.setattr(dispatch.time, "sleep", lambda s: None)
    ns = {**dict(dry_run=True, once=False, max_runs=None, max_prep=None, no_pull=True, interval=None), **flags}
    with pytest.raises(KeyboardInterrupt):
        dispatch.cmd_dispatch(argparse.Namespace(**ns))
    return got


def test_dispatcher_passes_saved_values_to_the_next_pass_and_keeps_its_tmux_session(work, monkeypatch, capsys):
    got = run_dispatcher(work, monkeypatch, lambda t: t.replace("max_runs = 3", "max_runs = 8")
                         .replace('tmux_session = "pl-work"', 'tmux_session = "other"'))
    assert [g[0] for g in got] == [3, 8]
    assert got[1][2] == "pl-work" and got[1][3] == {}
    assert "tmux_session" in capsys.readouterr().out


def test_an_explicit_flag_still_wins_over_saved_values(work, monkeypatch):
    got = run_dispatcher(work, monkeypatch, lambda t: t.replace("max_runs = 3", "max_runs = 8"), )
    assert got[1][0] == 8
    work.write_text(work.read_text().replace("max_runs = 8", "max_runs = 3"))
    C.load("work")
    got = run_dispatcher(work, monkeypatch, lambda t: t.replace("max_runs = 3", "max_runs = 9"), max_runs=3)
    assert [g[0] for g in got] == [3, 3]
    got = []
    def once(max_runs, *a, **k):
        got.append(max_runs)
        if len(got) == 1:
            work.write_text(work.read_text().replace("max_runs = 3", "max_runs = 8"))
        else:
            raise KeyboardInterrupt
    monkeypatch.setattr(dispatch, "dispatch_once", once)
    with pytest.raises(KeyboardInterrupt):
        dispatch.cmd_dispatch(argparse.Namespace(dry_run=True, once=False, max_runs=1, max_prep=None, no_pull=True, interval=1))
    assert got == [1, 1]


def test_a_broken_file_keeps_the_old_settings_and_the_dispatcher_alive(work, monkeypatch, capsys):
    got = run_dispatcher(work, monkeypatch, lambda t: t + '\n[loops.x]\naccount = "main"\n')
    assert got[1][0] == 3 and got[1][2] == "pl-work" and C.CONFIG_DIR == work.parent
    assert "not reloaded" in capsys.readouterr().out


def test_the_tmux_warning_prints_once(work, monkeypatch, capsys):
    monkeypatch.setattr(dispatch, "_WARNED_TMUX", set())
    for _ in range(2):
        work.write_text(work.read_text().replace('tmux_session = "pl-work"', 'tmux_session = "other"'))
        dispatch.reload_config()
    assert capsys.readouterr().out.count("tmux_session changed") == 1


# ---------- more validation and the Activity reader ----------

def test_save_refuses_a_loop_without_a_prompt(work):
    before = work.read_bytes()
    doc = doc_of(work)
    doc.add("loops", {"x": {"account": "main"}})
    with pytest.raises(SystemExit) as e:
        C.save(doc)
    assert "prompt" in str(e.value) and work.read_bytes() == before


def test_save_refuses_a_bad_attention_command_and_an_empty_github_project(work, fake_home):
    for change, words in [
        (lambda d: d.add("paths", {"attention_cmd": "~/nope/nothing"}), "attention_cmd"),
        (lambda d: d.__setitem__("tracker", {"type": "github-project", "owner": ""}), "owner"),
        (lambda d: d.__setitem__("tracker", {"type": "github-project", "owner": "octo", "number": ""}), "number"),
    ]:
        doc = doc_of(work)
        change(doc)
        with pytest.raises(SystemExit) as e:
            C.save(doc)
        assert words in str(e.value)


def test_token_reference_never_expands_into_the_file_or_the_screen(work, monkeypatch, gh_signed_in):
    monkeypatch.setenv("TOKEN", "leaked")
    async def go():
        app = PlApp(snapshot_provider=lambda: app_data([]), interval=3600)
        async with app.run_test(size=(176, 48)) as pilot:
            await settle(pilot)
            app.action_tab("settings")
            await settle(pilot)
            app.query_one(settings.SettingsView).action_save()
            await settle(pilot)
            return screen_text(app)
    import asyncio
    screen = asyncio.run(go())
    C.save(doc_of(work))
    assert "leaked" not in work.read_text() and "leaked" not in screen
    assert 'token = "${TOKEN}"' in work.read_text()


def test_activity_reader_skips_bad_bytes_and_reads_only_the_newest_lines(tmp_path):
    from pl.tui import loops
    f = tmp_path / "events.jsonl"
    f.write_bytes(b'{"kind":"error"}\n\xff\xfe\nnot json\n' + b"".join(b'{"kind":"n","i":%d}\n' % i for i in range(900)))
    ev = loops.read_events(f)
    assert len(ev) == loops.MAX_EVENTS and ev[0]["i"] == 899 and all(isinstance(e, dict) for e in ev)
    f.write_bytes(b'{"kind":"error"}\n\xff\xfe\n')
    assert loops.read_events(f) == [{"kind": "error"}]
    assert loops.read_events(tmp_path / "missing") == []


@pytest.mark.parametrize("bad", ["interval = -1", 'interval = "x"'])
def test_a_bad_interval_on_reload_keeps_the_old_settings(work, monkeypatch, capsys, bad):
    got = run_dispatcher(work, monkeypatch, lambda t: t.replace("max_runs = 3", f"max_runs = 3\n{bad}"))
    assert got[1][0] == 3
    out = capsys.readouterr().out
    assert "not reloaded" in out and "interval" in out
