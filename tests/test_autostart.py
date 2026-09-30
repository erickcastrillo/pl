"""WP20: the console starts this profile's dispatcher, detached in tmux, once; a key starts or stops it by hand.
tmux is a fake script on PATH that logs its argv; the lock is the profile's real lock file or a fake check."""
import fcntl
import json
import sys

import pytest
import tomlkit

from pl import config as C
from pl import dispatch
from pl.tui.app import PlApp
from pl.tui.review import ConfirmScreen
from test_tui_views import fake_data

FAKE_TMUX = """#!{py}
import json, os, sys
with open(os.environ["FAKE_TMUX_LOG"], "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
cmd = sys.argv[1] if len(sys.argv) > 1 else ""
if cmd == "has-session":
    sys.exit(0 if os.environ.get("FAKE_TMUX_SESSION") == "1" else 1)
if cmd in ("new-session", "new-window") and os.environ.get("FAKE_TMUX_FAIL"):
    sys.stderr.write("no server running on /tmp/tmux-501/default\\n")
    sys.exit(1)
"""


@pytest.fixture(autouse=True)
def profile(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("PL_CONFIG_DIR", "PL_TMUX_SESSION", "GH_CONFIG_DIR"):
        monkeypatch.delenv(var, raising=False)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "tmux").write_text(FAKE_TMUX.format(py=sys.executable))
    (bin_dir / "tmux").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("FAKE_TMUX_LOG", str(tmp_path / "tmux.log"))
    d = tmp_path / ".pl-work"
    d.mkdir()
    (d / "config.toml").write_text("[dispatch]\nmax_runs = 1\n")
    C.load(config_dir=str(d))
    monkeypatch.setattr(dispatch, "LOCK_WAIT", 0.5)
    yield d
    for k, v in saved.items():
        setattr(C, k, v)


def calls(tmp_path):
    log = tmp_path / "tmux.log"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def starts(tmp_path):
    return [c for c in calls(tmp_path) if c[0] in ("new-session", "new-window")]


def lock_taken_by_fake_tmux(tmp_path):
    """A fake lock: the dispatcher 'takes' it as soon as tmux was asked to start it."""
    return lambda: bool(starts(tmp_path))


async def settle(pilot):
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


def header(app):
    return str(app.query_one("#header").render())


async def test_not_running_starts_one_detached_dispatcher_with_the_profile_env(tmp_path, profile, monkeypatch):
    monkeypatch.setattr(dispatch, "dispatcher_running", lock_taken_by_fake_tmux(tmp_path))
    app = PlApp(snapshot_provider=fake_data, autostart=True)
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        got = starts(tmp_path)
        assert got == [["new-session", "-d", "-s", "pl-work", "-n", "dispatch", "-e", f"PL_CONFIG_DIR={profile}",
                        "--", sys.executable, "-m", "pl", "dispatch"]]
        assert "dispatcher: started" in header(app)


async def test_session_exists_opens_a_window_not_a_session(tmp_path, profile, monkeypatch):
    monkeypatch.setenv("FAKE_TMUX_SESSION", "1")
    C.GH_CONFIG_DIR = profile / "gh"
    monkeypatch.setattr(dispatch, "dispatcher_running", lock_taken_by_fake_tmux(tmp_path))
    async with PlApp(snapshot_provider=fake_data, autostart=True).run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        assert starts(tmp_path) == [["new-window", "-d", "-t", "=pl-work:", "-n", "dispatch",
                                     "-e", f"PL_CONFIG_DIR={profile}", "-e", f"GH_CONFIG_DIR={profile / 'gh'}",
                                     "--", sys.executable, "-m", "pl", "dispatch"]]


async def test_running_dispatcher_means_no_tmux_call(tmp_path, profile):
    C.LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    held = open(C.LOCK_FILE, "a+")   # another "process" holds this profile's lock
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        app = PlApp(snapshot_provider=fake_data, autostart=True)
        async with app.run_test(size=(176, 48)) as pilot:
            await settle(pilot)
            assert calls(tmp_path) == []
            assert "dispatcher: started" not in header(app) and "failed" not in header(app)
    finally:
        held.close()


async def test_autostart_false_never_starts(tmp_path, profile, monkeypatch):
    (profile / "config.toml").write_text("[dispatch]\nautostart = false\n")
    C.load(config_dir=str(profile))
    monkeypatch.setattr(dispatch, "dispatcher_running", lock_taken_by_fake_tmux(tmp_path))
    async with PlApp(snapshot_provider=fake_data, autostart=True).run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        assert calls(tmp_path) == []


async def test_start_failure_shows_the_reason_and_the_app_lives(tmp_path, profile, monkeypatch):
    monkeypatch.setenv("FAKE_TMUX_FAIL", "1")
    monkeypatch.setattr(dispatch, "dispatcher_running", lambda: False)
    app = PlApp(snapshot_provider=fake_data, autostart=True)
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        assert app.is_running
        assert "dispatcher: failed to start" in header(app) and "no server running" in header(app)
        assert "Traceback" not in header(app)


async def test_started_but_lock_never_taken_is_a_failure(tmp_path, profile, monkeypatch):
    monkeypatch.setattr(dispatch, "dispatcher_running", lambda: False)
    app = PlApp(snapshot_provider=fake_data, autostart=True)
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        assert len(starts(tmp_path)) == 1
        assert "dispatcher: failed to start" in header(app)


async def test_paused_profile_starts_anyway_and_says_paused(tmp_path, profile, monkeypatch):
    C.PAUSE_FILE.write_text(json.dumps({"since": "2026-09-29T10:00:00+00:00"}))
    monkeypatch.setattr(dispatch, "dispatcher_running", lock_taken_by_fake_tmux(tmp_path))
    app = PlApp(snapshot_provider=fake_data, autostart=True)
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        assert len(starts(tmp_path)) == 1
        assert "dispatcher: started" in header(app) and "paused" in header(app)


async def test_stop_key_asks_then_sends_ctrl_c_to_this_profiles_dispatch_window_only(tmp_path, profile, monkeypatch):
    # fake lock: held until the first Ctrl-C reaches tmux
    monkeypatch.setattr(dispatch, "dispatcher_running", lambda: not any(c[0] == "send-keys" for c in calls(tmp_path)))
    app = PlApp(snapshot_provider=fake_data, autostart=False)
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        await pilot.press("D")
        await settle(pilot)
        assert isinstance(app.screen, ConfirmScreen) and "work" in app.screen.message
        assert calls(tmp_path) == []            # nothing sent before the answer
        await pilot.press("y")
        await settle(pilot)
        assert calls(tmp_path) == [["send-keys", "-t", "=pl-work:dispatch", "C-c"]]
        assert "dispatcher: stopped" in header(app)


async def test_stop_key_answered_no_sends_nothing(tmp_path, profile, monkeypatch):
    monkeypatch.setattr(dispatch, "dispatcher_running", lambda: True)
    async with PlApp(snapshot_provider=fake_data, autostart=False).run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        await pilot.press("D")
        await settle(pilot)
        await pilot.press("n")
        await settle(pilot)
        assert calls(tmp_path) == []


def test_stop_gives_up_after_five_ctrl_c(tmp_path, profile, monkeypatch):
    monkeypatch.setattr(dispatch, "dispatcher_running", lambda: True)
    note = dispatch.stop_dispatcher()
    assert calls(tmp_path) == [["send-keys", "-t", "=pl-work:dispatch", "C-c"]] * 5
    assert "still running" in note


async def test_start_key_starts_it_by_hand_even_with_autostart_off(tmp_path, profile, monkeypatch):
    (profile / "config.toml").write_text("[dispatch]\nautostart = false\n")
    C.load(config_dir=str(profile))
    monkeypatch.setattr(dispatch, "dispatcher_running", lock_taken_by_fake_tmux(tmp_path))
    app = PlApp(snapshot_provider=fake_data, autostart=True)
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        assert calls(tmp_path) == []
        await pilot.press("D")
        await settle(pilot)
        assert len(starts(tmp_path)) == 1 and "dispatcher: started" in header(app)


def test_real_lock_check_is_the_profiles_lock(tmp_path, profile):
    assert dispatch.dispatcher_running() is False
    C.LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(C.LOCK_FILE, "a+") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert dispatch.dispatcher_running() is True


@pytest.mark.parametrize("value", ["yes", 1, 0, "false"])
def test_validate_rejects_a_non_bool_autostart(profile, value):
    doc = tomlkit.parse("[dispatch]\n")
    doc["dispatch"]["autostart"] = value
    assert any("dispatch.autostart" in e for e in C.validate(doc))


@pytest.mark.parametrize("value", [True, False])
def test_validate_accepts_a_bool_autostart(profile, value):
    doc = tomlkit.parse("[dispatch]\n")
    doc["dispatch"]["autostart"] = value
    assert not any("autostart" in e for e in C.validate(doc))


async def test_started_note_names_the_dispatchers_pid(tmp_path, profile, monkeypatch):
    C.LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    C.LOCK_FILE.write_text("pid 4242 on h since now")
    monkeypatch.setattr(dispatch, "dispatcher_running", lock_taken_by_fake_tmux(tmp_path))
    app = PlApp(snapshot_provider=fake_data, autostart=True)
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        assert "dispatcher: started (pid 4242)" in header(app)


async def test_autostart_and_the_key_together_start_one_dispatcher(tmp_path, profile, monkeypatch):
    """WP45: autostart plus D pressed while it starts never opens a second dispatch window."""
    monkeypatch.setattr(dispatch, "dispatcher_running", lock_taken_by_fake_tmux(tmp_path))
    app = PlApp(snapshot_provider=fake_data, autostart=True)
    async with app.run_test(size=(176, 48)) as pilot:
        app.dispatcher_job("start")
        app.dispatcher_job("start")
        await settle(pilot)
        assert len(starts(tmp_path)) == 1
