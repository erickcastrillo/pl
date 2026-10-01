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
