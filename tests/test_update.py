"""Update check: a fake git on PATH, a temporary HOME, no network. pl only tells; it never installs."""
import json
import sys
import time

import pytest

from pl import cli, update
from pl import config as C
from test_tui_views import Provider, screen_text, settle

INSTALLED = "0.2.0"


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PL_NO_UPDATE_CHECK", raising=False)
    for var in ("PL_CONFIG_DIR", "PL_TMUX_SESSION"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(update, "__version__", INSTALLED)
    d = tmp_path / ".pl-work"
    d.mkdir()
    (d / "config.toml").write_text("")
    C.load(config_dir=str(d))
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


@pytest.fixture
def git(tmp_path, monkeypatch):
    """A fake git: prints the tags written to tags.txt, logs every call, and sleeps or fails when told to."""
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    log, tags = tmp_path / "git.log", tmp_path / "tags.txt"
    tags.write_text("")
    script = bin_ / "git"
    script.write_text(f"""#!/bin/sh
echo "$@" >> {log}
if [ -f {tmp_path}/sleep ]; then sleep 10; fi
if [ -f {tmp_path}/fail ]; then exit 128; fi
while read t; do echo "deadbeef	refs/tags/$t"; done < {tags}
""")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_}:/usr/bin:/bin")

    class Git:
        def tags(self, *names):
            tags.write_text("".join(f"{n}\n" for n in names))

        def calls(self):
            return log.read_text().splitlines() if log.exists() else []

        def sleep(self):
            (tmp_path / "sleep").write_text("")

        def fail(self):
            (tmp_path / "fail").write_text("")
    return Git()


def test_a_newer_tag_gives_the_notice(git):
    git.tags("v0.1.0", "v0.2.0", "v0.3.0", "not-a-release", "v1.0.0-rc1")
    assert update.check() == "v0.3.0"
    assert update.notice(update.check()) == \
        "pl v0.3.0 is available (you have v0.2.0) — press U for how to update"
    assert git.calls() == ["ls-remote --tags --refs https://github.com/erickcastrillo/pl"]


@pytest.mark.parametrize("tags", [("v0.2.0",), ("v0.1.0", "v0.1.9"), ()])
def test_the_same_or_an_older_tag_gives_no_notice(git, tags):
    git.tags(*tags)
    assert update.notice(update.check()) is None


def test_versions_compare_as_numbers(git, monkeypatch):
    monkeypatch.setattr(update, "__version__", "0.9.0")
    git.tags("v0.9.0", "v0.10.0", "v0.2.0")
    assert update.check() == "v0.10.0"
    assert update.notice("v0.10.0").startswith("pl v0.10.0 is available (you have v0.9.0)")


def test_the_cache_skips_a_second_check_within_a_day(git, env):
    git.tags("v0.3.0")
    assert update.check() == "v0.3.0"
    cache = json.loads((env / ".local" / "state" / "pl" / "update.json").read_text())
    assert cache["latest"] == "v0.3.0" and cache["checked_at"] > 0
    git.tags("v0.4.0")
    assert update.check() == "v0.3.0" and len(git.calls()) == 1
    assert update.check(now=time.time() + 86400 + 1) == "v0.4.0" and len(git.calls()) == 2


def test_an_error_is_silent_and_still_counts_as_the_day_s_check(git):
    git.fail()
    assert update.check() is None and update.notice(None) is None
    assert update.check() is None and len(git.calls()) == 1


def test_a_timeout_is_silent_and_start_up_does_not_wait(git, monkeypatch):
    monkeypatch.setattr(update, "TIMEOUT", 0.5)
    git.sleep()
    git.tags("v0.3.0")
    t0 = time.monotonic()
    assert update.check() is None
    assert time.monotonic() - t0 < 3    # the sleeping child is killed too, not waited for
    got = []
    t0 = time.monotonic()
    th = update.start_background(got.append)
    assert time.monotonic() - t0 < 0.2  # start-up returns at once
    th.join(5)
    assert got == []


def test_the_background_check_hands_over_the_notice(git):
    git.tags("v0.3.0")
    got = []
    update.start_background(got.append).join(5)
    assert got == ["pl v0.3.0 is available (you have v0.2.0) — press U for how to update"]


@pytest.mark.parametrize("how", ["env", "config"])
def test_the_off_switch_makes_no_call(git, env, monkeypatch, how):
    git.tags("v0.3.0")
    if how == "env":
        monkeypatch.setenv("PL_NO_UPDATE_CHECK", "1")
    else:
        (env / ".pl-work" / "config.toml").write_text("[updates]\ncheck = false\n")
        C.load(config_dir=str(env / ".pl-work"))
    assert update.check() is None
    assert update.start_background(lambda n: None) is None
    assert git.calls() == []


def run_cli(monkeypatch, capsys, *argv):
    monkeypatch.setattr(sys, "argv", ["pl", *argv])
    try:
        cli.main()
    except SystemExit:
        pass
    return capsys.readouterr().out


def test_pl_version_and_pl_update_check_print_the_notice(git, env, monkeypatch, capsys):
    git.tags("v0.3.0")
    for argv in (["--version"], ["update", "--check"]):
        out = run_cli(monkeypatch, capsys, *argv)
        assert "pl 0.2.0" in out
        assert "pl v0.3.0 is available (you have v0.2.0) — run pl update for how to update" in out
    git.tags("v0.2.0")
    (env / ".local" / "state" / "pl" / "update.json").unlink()
    assert "available" not in run_cli(monkeypatch, capsys, "--version")


def test_pl_update_prints_the_commands_and_runs_nothing(git, monkeypatch, capsys):
    import subprocess
    ran = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: ran.append(a))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: ran.append(a))
    out = run_cli(monkeypatch, capsys, "update")
    assert "git pull" in out and "uv tool install --force --reinstall ." in out
    assert "pl never runs these for you" in out
    assert ran == [] and git.calls() == []


async def test_the_console_shows_the_notice_and_u_shows_the_commands(git, monkeypatch):
    from pl.tui.app import PlApp, UpdateScreen
    git.tags("v0.3.0")
    copied = []
    monkeypatch.setattr(PlApp, "copy_to_clipboard", lambda self, t: copied.append(t))
    monkeypatch.setattr("pl.tui.dashboard._copy_run", lambda argv, t: None)   # no real pbcopy
    app = PlApp(snapshot_provider=Provider(), updates=True)
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        assert any("pl v0.3.0 is available (you have v0.2.0) — press U" in n.message
                   for n in app._notifications)
        await pilot.press("U")
        await settle(pilot)
        assert isinstance(app.screen, UpdateScreen)
        text = screen_text(app)
        assert "git pull" in text and "uv tool install --force --reinstall" in text and "v0.3.0" in text
        await pilot.press("c")
        assert copied == [update.COMMANDS]
        await pilot.press("escape")
        await settle(pilot)
        assert not isinstance(app.screen, UpdateScreen)
    assert len(git.calls()) == 1


async def test_test_consoles_do_not_check(git):
    from pl.tui.app import PlApp
    git.tags("v0.3.0")
    async with PlApp(snapshot_provider=Provider()).run_test(size=(176, 48)) as pilot:
        await settle(pilot)
    assert git.calls() == []


def test_build_id_changes_when_an_installed_file_changes_but_not_for_compiled_caches(tmp_path, monkeypatch):
    pkg = tmp_path / "pkg"
    (pkg / "__pycache__").mkdir(parents=True)
    (pkg / "__init__.py").write_text("x = 1\n")
    monkeypatch.setattr(update, "PKG", pkg)
    a = update.build_id(fresh=True)
    assert a.startswith(f"{INSTALLED}-") and update.build_id(fresh=True) == a
    (pkg / "__pycache__" / "x.pyc").write_text("cache")
    assert update.build_id(fresh=True) == a
    (pkg / "__init__.py").write_text("x = 22\n")   # a reinstall of the same version
    assert update.build_id(fresh=True) != a


def test_build_id_is_cached_per_process(monkeypatch):
    monkeypatch.setattr(update, "_BUILD", {})
    first = update.build_id()
    monkeypatch.setattr(update, "build_id_of", lambda root: "changed")
    assert update.build_id() == first and update.build_id(fresh=True) != first


def test_new_build_waits_for_an_install_to_settle(tmp_path, monkeypatch):
    reads = iter(["b1", "b2"])
    monkeypatch.setattr(update, "_BUILD", {"id": "b0"})
    monkeypatch.setattr(update, "build_id_of", lambda root: next(reads))
    monkeypatch.setattr(update, "_sleep", lambda s: None)
    assert update.new_build() is None   # still changing: an install in progress
    monkeypatch.setattr(update, "build_id_of", lambda root: "b3")
    assert update.new_build() == "b3"
    monkeypatch.setattr(update, "build_id_of", lambda root: "b0")
    assert update.new_build() is None


def test_pl_update_says_pl_restarts_itself(capsys):
    update.cmd_update(type("A", (), {"check": False})())
    out = capsys.readouterr().out
    assert "restarts itself" in out and "press D" not in out


def test_importable_runs_the_new_install_and_needs_ok(monkeypatch):
    import subprocess
    import sys
    calls = []
    for out, ok in (("ok\n", True), ("", False)):
        monkeypatch.setattr(update, "_run", lambda argv, timeout=None, out=out: calls.append((argv, timeout)) or out)
        assert update.importable() is ok
    assert calls[0] == ([sys.executable, "-c", "import pl.cli, pl.manager, pl.dispatch; print('ok')"], 60)

    def hang(argv, timeout=None):
        raise subprocess.TimeoutExpired(argv, timeout)
    monkeypatch.setattr(update, "_run", hang)
    assert update.importable() is False


def test_restart_build_checks_a_build_once_and_never_retries_a_bad_one(monkeypatch):
    monkeypatch.setattr(update, "_BUILD", {"id": "b0"})
    monkeypatch.setattr(update, "_BAD", set())
    monkeypatch.setattr(update, "_sleep", lambda s: None)
    monkeypatch.setattr(update, "build_id_of", lambda root: "b1")
    checks, warned = [], []
    monkeypatch.setattr(update, "importable", lambda: checks.append(1) and False)
    assert update.restart_build(warn=warned.append) is None
    assert update.restart_build(warn=warned.append) is None
    assert checks == [1] and len(warned) == 1 and "b1" in warned[0]
    monkeypatch.setattr(update, "build_id_of", lambda root: "b2")
    monkeypatch.setattr(update, "importable", lambda: True)
    assert update.restart_build(warn=warned.append) == "b2"


def test_an_asked_restart_needs_a_present_settled_build_even_when_it_is_the_same(monkeypatch):
    monkeypatch.setattr(update, "_BUILD", {"id": "b0"})
    monkeypatch.setattr(update, "_BAD", set())
    monkeypatch.setattr(update, "_sleep", lambda s: None)
    monkeypatch.setattr(update, "importable", lambda: True)
    monkeypatch.setattr(update, "build_id_of", lambda root: "b0")
    assert update.restart_build() is None and update.restart_build(asked=True) == "b0"
    monkeypatch.setattr(update, "build_id_of", lambda root: None)   # an install in progress
    assert update.restart_build(asked=True) is None
    reads = iter(["b0", "b1"])
    monkeypatch.setattr(update, "build_id_of", lambda root: next(reads))
    assert update.restart_build(asked=True) is None


def test_build_id_of_reads_the_version_from_the_files_under_root(tmp_path):
    (tmp_path / "__init__.py").write_text('__version__ = "9.8.7"\n')
    assert update.build_id_of(tmp_path).startswith("9.8.7-") and update.__version__ != "9.8.7"
    (tmp_path / "__init__.py").write_text("")   # no readable version: the running one stands in
    assert update.build_id_of(tmp_path).startswith(f"{update.__version__}-")


@pytest.fixture
def exits(monkeypatch):
    """PlApp.exit recorded instead of run: the test console stays up to be looked at."""
    from pl.tui.app import PlApp
    calls = []
    monkeypatch.setattr(PlApp, "exit", lambda self, *a, **k: calls.append(1))
    return calls


async def test_the_console_restarts_itself_on_a_new_build_when_nothing_is_open(monkeypatch, exits):
    from pl.tui.app import PlApp
    monkeypatch.setattr(update, "restart_build", lambda asked=False, warn=print: "b9")
    app = PlApp(snapshot_provider=Provider(), restarts=True)
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        app.build_job()
        await settle(pilot)
        assert app.restart_into == "b9" and exits == [1]


async def test_the_console_reads_its_own_build_at_start(monkeypatch):
    from pl.tui.app import PlApp
    monkeypatch.setattr(update, "_BUILD", {})
    monkeypatch.setattr(update, "build_id_of", lambda root: "b0")
    async with PlApp(snapshot_provider=Provider(), restarts=True).run_test(size=(176, 48)) as pilot:
        await settle(pilot)
    assert update._BUILD == {"id": "b0"}


async def test_a_draft_or_an_open_dialog_waits_and_N_restarts_by_hand(monkeypatch, exits):
    from pl.tui.app import PlApp, WhatsNewScreen
    from pl.tui.assistant import AssistantInput
    monkeypatch.setattr(update, "restart_build", lambda asked=False, warn=print: "b9")
    app = PlApp(snapshot_provider=Provider(), restarts=True)
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        app.query_one(AssistantInput).value = "half a question"
        app.build_job()
        await settle(pilot)
        assert exits == [] and app.restart_into is None
        notes = [n.message for n in app._notifications if "pl was updated" in n.message]
        assert len(notes) == 1 and "press N" in notes[0]
        app.build_job()   # still busy: told once
        await settle(pilot)
        assert len([n for n in app._notifications if "pl was updated" in n.message]) == 1
        app.query_one(AssistantInput).value = ""
        app.push_screen(WhatsNewScreen([]))
        await settle(pilot)
        app.build_job()
        await settle(pilot)
        assert exits == []
        await pilot.press("escape")
        await pilot.press("N")
        await settle(pilot)
        assert app.restart_into == "b9" and exits == [1]


async def test_a_build_that_does_not_import_is_told_and_the_console_keeps_running(monkeypatch, exits):
    from pl.tui.app import PlApp

    def broken(asked=False, warn=print):
        warn("pl build b9 is installed but does not import; keeping the running version until the next install")
    monkeypatch.setattr(update, "restart_build", broken)
    app = PlApp(snapshot_provider=Provider(), restarts=True)
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        app.build_job()
        await settle(pilot)
        assert exits == [] and app.restart_into is None
        assert any("does not import" in n.message for n in app._notifications)


async def test_test_consoles_do_not_restart(monkeypatch, exits):
    from pl.tui.app import PlApp
    app = PlApp(snapshot_provider=Provider())
    assert app.check_build is False


def test_the_console_execs_the_same_command_after_a_restart(monkeypatch):
    import argparse
    import os
    from pl import watch
    from pl.tui import app as tui_app
    runs, execs = [], []

    class FakeApp:
        def __init__(self, interval=None):
            self.restart_into = None

        def run(self):
            runs.append(1)
            self.restart_into = "b9" if len(runs) == 1 else None

    def fail(path, argv):
        execs.append((path, argv))
        raise OSError("no such file")
    monkeypatch.setattr(tui_app, "PlApp", FakeApp)
    monkeypatch.setattr(os, "execv", fail)
    monkeypatch.setattr(sys, "orig_argv", [sys.executable, "/bin/pl", "--profile", "work"])
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(update, "_BAD", set())
    watch.cmd_watch(argparse.Namespace(interval=None, once=False, plain=False))
    assert execs == [(sys.executable, [sys.executable, "/bin/pl", "--profile", "work"])]
    assert runs == [1, 1] and update._BAD == {"b9"}   # a failed exec opens this version's console again
