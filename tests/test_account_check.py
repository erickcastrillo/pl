"""Health checks of parked accounts: a cheap non-interactive call says whether the account works again."""
import argparse
import fcntl
import json
import subprocess
import time
import types
from datetime import datetime, timezone

import pytest

from pl import accounts, alerts, commands, harnesses
from pl import config as C
from pl.util import parse_iso

CREDITS = "You're out of usage credits · /upgrade to keep using Claude Code"


@pytest.fixture(autouse=True)
def two_profiles(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PL_MACHINE_DIR", str(tmp_path / "machine"))
    monkeypatch.delenv("PL_CONFIG_DIR", raising=False)
    for d in ("acct-f", "acct-g", "acct-x", "acct-c"):
        (tmp_path / d).mkdir()
    (tmp_path / "link-f").symlink_to(tmp_path / "acct-f")   # same folder, another spelling
    for name, body in (("a", f'[accounts.main]\nconfig_dir = "{tmp_path}/acct-f"\n'
                             f'[accounts.agy]\nharness = "antigravity"\nconfig_dir = "{tmp_path}/acct-x"\n'
                             f'[accounts.cx]\nharness = "codex"\nconfig_dir = "{tmp_path}/acct-c"\n'),
                       ("b", f'[accounts.work]\nconfig_dir = "{tmp_path}/link-f"\n'
                             f'[accounts.solo]\nconfig_dir = "{tmp_path}/acct-g"\n')):
        (tmp_path / f".pl-{name}").mkdir()
        (tmp_path / f".pl-{name}" / "config.toml").write_text(body)
    harnesses._CACHE.clear()
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)
    harnesses._CACHE.clear()


@pytest.fixture
def probe(monkeypatch):
    """The fake harness call: answers what the test sets, records every call."""
    calls, answer = [], {"out": "ok\n", "err": "", "code": 0, "raise": None}

    def run(argv, env, timeout, cwd):
        calls.append({"argv": argv, "env": env, "timeout": timeout, "cwd": cwd})
        if answer["raise"]:
            raise answer["raise"]
        return types.SimpleNamespace(stdout=answer["out"], stderr=answer["err"], returncode=answer["code"])
    monkeypatch.setattr(accounts, "_probe", run)
    return calls, answer


def as_profile(name):
    C.load(name)
    harnesses._CACHE.clear()


def _machine():
    return accounts._machine()


def _rec(name):
    return accounts.parking(name)


def _due_now(name):
    """Move the account's next check into the past, as if its interval went by."""
    folder = accounts._folder(name)
    past = datetime.fromtimestamp(time.time() - 5, timezone.utc).isoformat(timespec="seconds")
    accounts._update_machine(lambda d: d[folder].__setitem__("next_check", past))
    if name in accounts.profile_state():
        accounts._update(C.PROFILE_STATE, lambda st: st[name].__setitem__("next_check", past))


def test_a_park_without_a_reset_time_schedules_the_first_check_in_10_minutes(two_profiles):
    as_profile("a")
    accounts.mark_exhausted("main", CREDITS)
    r = _rec("main")
    assert abs(parse_iso(r["next_check"]) - (time.time() + 600)) < 5
    assert r["checks"] == 0 and "checked_at" not in r


def test_a_park_with_a_reset_time_is_not_checked(two_profiles, probe):
    as_profile("a")
    accounts.mark_exhausted("main", "You've hit your limit · resets 9pm")
    assert _rec("main")["next_check"] is None
    assert accounts.check_parked() == [] and probe[0] == []


def test_no_check_before_it_is_due(two_profiles, probe):
    as_profile("a")
    accounts.mark_exhausted("main", CREDITS)
    assert accounts.check_parked() == [] and probe[0] == []


def test_a_working_account_is_unparked_everywhere(two_profiles, probe, monkeypatch):
    calls, _ = probe
    as_profile("b")
    accounts.mark_exhausted("work", CREDITS)            # b parks the shared folder
    as_profile("a")
    accounts.mark_exhausted("main", CREDITS)            # a parks it too, in its own file
    alerts.open("account_parked:main", "warn", "Account main is parked: usage limit", "x")
    seen = []
    monkeypatch.setattr(accounts.events, "emit", lambda kind, card=None, **f: seen.append((kind, f)))
    _due_now("main")
    assert accounts.check_parked() == [("main", "ok")]
    c = calls[0]
    assert c["argv"] == ["claude", "-p", accounts.CHECK_PROMPT, "--no-session-persistence",
                         "--disable-slash-commands", "--tools", ""]
    assert c["env"]["CLAUDE_CONFIG_DIR"] == str(two_profiles / "acct-f") and c["timeout"] == accounts.CHECK_TIMEOUT
    assert c["cwd"] == str(C.STATE_DIR)
    assert accounts.exhausted_profiles() == set() and _machine() == {}
    assert not alerts.get("account_parked:main") or alerts.get("account_parked:main").get("resolved_at")
    assert ("account_check", {"account": "main", "result": "ok"}) in seen
    as_profile("b")
    assert accounts.exhausted_profiles() == set()


def test_still_limited_backs_off_10_30_60_60_and_stays_parked(two_profiles, probe):
    calls, answer = probe
    answer.update(out="", err=CREDITS, code=1)
    as_profile("a")
    accounts.mark_exhausted("main", CREDITS)
    for want in (1800, 3600, 3600):
        _due_now("main")
        assert accounts.check_parked() == [("main", "limited")]
        r = _rec("main")
        assert abs(parse_iso(r["next_check"]) - (time.time() + want)) < 5
        assert parse_iso(r["until"]) > parse_iso(r["next_check"])   # the timer never unparks it before the check
        assert r["check_result"] == "still limited" and r["checked_at"]
        assert "main" in accounts.exhausted_profiles()
    assert len(calls) == 3 and _rec("main")["checks"] == 3


def test_still_limited_with_a_printed_reset_parks_until_it(two_profiles, probe):
    _, answer = probe
    answer.update(out="You've hit your limit · resets Oct 9 at 9pm", code=1)
    as_profile("a")
    accounts.mark_exhausted("main", CREDITS)
    _due_now("main")
    accounts.check_parked()
    r = _rec("main")
    want = accounts.reset_at(answer["out"], datetime.now()).timestamp() + 60
    assert parse_iso(r["until"]) == want and r["next_check"] is None


@pytest.mark.parametrize("ans, result", [({"code": 2, "out": "boom"}, "error: exit 2"),
                                         ({"raise": subprocess.TimeoutExpired("claude", 60)}, "error: no answer in 60 s"),
                                         ({"raise": FileNotFoundError(2, "No such file")}, "error: cannot run claude")])
def test_an_error_is_recorded_and_retried_at_the_next_interval_without_unparking(two_profiles, probe, ans, result):
    _, answer = probe
    answer.update(ans)
    as_profile("a")
    accounts.mark_exhausted("main", CREDITS)
    until = _rec("main")["until"]
    _due_now("main")
    assert accounts.check_parked() == [("main", "error")]
    r = _rec("main")
    assert r["check_result"].startswith(result) and r["until"] == until   # the timer still applies
    assert abs(parse_iso(r["next_check"]) - (time.time() + 1800)) < 5
    assert "main" in accounts.exhausted_profiles()


def test_a_check_another_process_holds_is_skipped(two_profiles, probe):
    as_profile("a")
    accounts.mark_exhausted("main", CREDITS)
    _due_now("main")
    f = accounts._check_lock_path("main")
    f.parent.mkdir(parents=True, exist_ok=True)
    with open(f, "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert accounts.check_parked() == [] and probe[0] == []
    assert accounts.check_parked() == [("main", "ok")]


def test_a_second_profile_on_the_same_folder_does_not_check_again(two_profiles, probe):
    _, answer = probe
    answer.update(out=CREDITS, code=1)
    as_profile("a")
    accounts.mark_exhausted("main", CREDITS)
    _due_now("main")
    accounts.check_parked()
    as_profile("b")
    assert accounts.check_parked() == [] and len(probe[0]) == 1   # its next check is shared machine-wide


def test_codex_has_a_read_only_check_and_antigravity_keeps_the_timer(two_profiles, probe):
    calls, _ = probe
    as_profile("a")
    accounts.mark_exhausted("agy", CREDITS)
    accounts.mark_exhausted("cx", CREDITS)
    _due_now("agy")
    _due_now("cx")
    assert accounts.check_parked() == [("cx", "ok")]
    assert calls[0]["argv"] == ["codex", "exec", "--skip-git-repo-check", "--ephemeral", "--sandbox", "read-only",
                                accounts.CHECK_PROMPT]
    assert calls[0]["env"]["CODEX_HOME"] == str(two_profiles / "acct-c")
    assert accounts.exhausted_profiles() == {"agy"}


def test_a_park_from_before_checks_existed_is_checked_on_the_next_pass(two_profiles, probe):
    as_profile("a")
    accounts.mark_exhausted("main", CREDITS)
    folder = accounts._folder("main")
    accounts._update_machine(lambda d: [d[folder].pop(k) for k in ("next_check", "checks")])
    accounts._update(C.PROFILE_STATE, lambda st: [st["main"].pop(k) for k in ("next_check", "checks")])
    assert accounts.check_parked() == [("main", "ok")]


def test_account_checks_empty_turns_checks_off(two_profiles, probe):
    as_profile("a")
    C.DISPATCH = {**C.DISPATCH, "account_checks": []}
    accounts.mark_exhausted("main", CREDITS)
    assert _rec("main")["next_check"] is None
    _due_now("main")
    assert accounts.check_parked() == [] and probe[0] == []


def test_account_checks_setting_sets_the_intervals_and_is_validated(two_profiles, probe):
    _, answer = probe
    answer.update(out=CREDITS, code=1)
    as_profile("a")
    C.DISPATCH = {**C.DISPATCH, "account_checks": [5, 15]}
    accounts.mark_exhausted("main", CREDITS)
    assert abs(parse_iso(_rec("main")["next_check"]) - (time.time() + 300)) < 5
    _due_now("main")
    accounts.check_parked()
    assert abs(parse_iso(_rec("main")["next_check"]) - (time.time() + 900)) < 5
    import tomlkit
    for bad in ('[5, "x"]', "[0]", "5", "[true]"):
        assert any("account_checks" in e for e in C.validate(tomlkit.parse(f"[dispatch]\naccount_checks = {bad}\n"))), bad
    assert C.validate(tomlkit.parse("[dispatch]\naccount_checks = [10, 30, 60]\n")) == []


def test_pl_accounts_shows_the_last_check_and_the_next_one(two_profiles, probe, capsys):
    _, answer = probe
    answer.update(out=CREDITS, code=1)
    as_profile("a")
    accounts.mark_exhausted("main", CREDITS)
    capsys.readouterr()
    commands.cmd_profiles(argparse.Namespace(reset=None))
    out = capsys.readouterr().out
    assert "not checked yet; first check " in out
    _due_now("main")
    accounts.check_parked()
    commands.cmd_profiles(argparse.Namespace(reset=None))
    out = capsys.readouterr().out
    r = _rec("main")
    assert f"last check {r['checked_at'][11:16]} UTC: still limited; next check {r['next_check'][11:16]} UTC" in out
    assert "out of usage credits" in out   # the reason


def test_the_parked_alert_says_the_last_check_result(two_profiles, probe, monkeypatch):
    _, answer = probe
    answer.update(out=CREDITS, code=1)
    notes = []
    monkeypatch.setattr(accounts, "notify", lambda t, m: notes.append((t, m)))
    as_profile("a")
    accounts.mark_exhausted("main", CREDITS)
    _due_now("main")
    accounts.check_parked()
    a = alerts.get("account_parked:main")
    assert a and "last check" in a["fix"] and "still limited" in a["fix"]
    assert notes and "main" in notes[0][0]


def test_the_check_never_writes_the_harness_output(two_profiles, probe):
    _, answer = probe
    answer.update(out="SECRET-ANSWER " + CREDITS, code=1)
    as_profile("a")
    accounts.mark_exhausted("main", CREDITS)
    _due_now("main")
    accounts.check_parked()
    blob = json.dumps(_machine()) + C.PROFILE_STATE.read_text()
    assert "SECRET-ANSWER" not in blob
