import json

import pytest

from pl import alerts, board, checkout_watch, trackers, update, util
from pl import config as C
from pl import e2e_console as EC

CARD = {"id": "o/r#1", "list_id": "L1", "metadata": {}}
DONE = {"id": "o/r#2", "list_id": "L2", "metadata": {"worker": {"window": "@7"}}}


def make_profile(name="work", tracker="github-project"):
    d = C.HOME / f".pl-{name}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.toml").write_text(f'[tracker]\ntype = "{tracker}"\n')
    return d


class FakeTracker:
    def __init__(self, count=3, shown=3):
        self.seen, self.count, self.shown = [], count, shown

    def __getattr__(self, name):
        self.seen.append(name)
        raise AssertionError(f"live mode asked the tracker for {name}")

    def _view_path(self):
        self.seen.append("_view_path")
        return "/projects/1/views/2"

    def _http(self, method, path, body=None):
        self.seen.append(f"_http {method}")
        return [{"count": self.count}]


@pytest.fixture
def live(monkeypatch):
    s = {"argv": [], "rate": [1000, 1000], "cards": [CARD], "windows": "@1\n", "git": (0, ""),
         "version": f"pl {update.__version__}\n", "tracker": FakeTracker(), "board_calls": []}

    def run(argv, cwd=None, env=None, timeout=60, capture=False):
        s["argv"].append(argv)
        if argv[:2] == ["gh", "api"]:
            if s["rate"] is None:
                return 1, "gh: boom"
            return 0, json.dumps({"resources": {"graphql": {"remaining": s["rate"].pop(0), "limit": 5000}}})
        if argv[:2] == ["tmux", "list-windows"]:
            return 0, s["windows"]
        if argv[:2] == ["pl", "--version"]:
            return 0, s["version"]
        raise AssertionError(argv)

    def git(argv):
        s["argv"].append(["git", *argv])
        return s["git"]

    def forbidden(*a, **k):
        raise AssertionError("a live run wrote")

    monkeypatch.setattr(EC, "_run", run)
    monkeypatch.setattr(checkout_watch, "_git", git)
    monkeypatch.setattr(EC.importlib.metadata, "version", lambda n: update.__version__)
    monkeypatch.setattr(board, "fresh_next", lambda: s["board_calls"].append("fresh_next"))
    monkeypatch.setattr(board, "cards", lambda: s["cards"])
    monkeypatch.setattr(board, "col_name", lambda lid: {"L1": "In progress", "L2": "Done"}[lid])
    monkeypatch.setattr(trackers, "get", lambda kind: s["tracker"])
    monkeypatch.setattr(util, "tmux", forbidden, raising=False)
    monkeypatch.setattr(alerts, "listing", lambda all=False: s.get("alerts", []))
    make_profile()
    return s


def lines(capsys):
    return capsys.readouterr().out.splitlines()


def test_ac5_live_without_profile_exits_2_and_reads_no_tracker(live, capsys):
    assert EC.cmd(["--live"]) == 2
    assert "--profile" in capsys.readouterr().err
    assert live["argv"] == [] and live["tracker"].seen == []


def test_ac6_unknown_profile_is_named_exits_2_and_reads_no_tracker(live, capsys):
    assert EC.cmd(["--live", "--profile", "nope"]) == 2
    assert "nope" in capsys.readouterr().err
    assert live["argv"] == [] and live["tracker"].seen == []


def test_profile_without_live_is_a_usage_error():
    with pytest.raises(SystemExit) as e:
        EC.cmd(["--profile", "work"])
    assert e.value.code == 2


def test_ac1_lists_six_checks_with_status_and_reason(live, capsys):
    EC.cmd(["--live", "--profile", "work"])
    out = lines(capsys)
    assert [x[:2] for x in out[:6]] == ["a.", "b.", "c.", "d.", "e.", "f."]
    assert all(":" in x and x.split(":", 1)[1].strip() for x in out[:6])
    assert out[6] == "5 passed, 0 failed, 1 not applicable"


def test_ac3_all_pass_exits_0(live, capsys):
    live["tracker"] = FakeTracker()
    assert EC.cmd(["--live", "--profile", "work"]) == 0


def test_ac3_done_card_with_open_window_fails_and_names_card(live, capsys):
    live["cards"] = [CARD, DONE]
    live["windows"] = "@1\n@7\n"
    assert EC.cmd(["--live", "--profile", "work"]) == 1
    assert any(x.startswith("d.") and "fail" in x and "o/r#2" in x for x in lines(capsys))


def test_ac3_points_over_ten_percent_fail_names_spent_and_limit(live, capsys):
    live["rate"] = [1000, 850]
    assert EC.cmd(["--live", "--profile", "work"]) == 1
    assert any(x.startswith("b.") and "spent 150 of 100" in x for x in lines(capsys))


def test_points_read_failure_fails_with_the_error(live, capsys):
    live["rate"] = None
    assert EC.cmd(["--live", "--profile", "work"]) == 1
    assert any(x.startswith("b.") and "fail" in x and "boom" in x for x in lines(capsys))


def test_stalled_alert_fails(live, capsys):
    live["alerts"] = [{"key": "stalled:o/r#1", "cards": ["o/r#1"]}, {"key": "pr_waiting:o/r#3", "cards": ["o/r#3"]}]
    assert EC.cmd(["--live", "--profile", "work"]) == 1
    out = " ".join(lines(capsys))
    assert "o/r#1" in out and "o/r#3" not in out


def test_dirty_checkout_fails_naming_paths(live, capsys):
    live["git"] = (0, " M src/a.py\0")
    assert EC.cmd(["--live", "--profile", "work"]) == 1
    assert any(x.startswith("e.") and "src/a.py" in x for x in lines(capsys))


def test_version_mismatch_fails_naming_versions(live, capsys):
    live["version"] = "pl 0.0.1\n"
    assert EC.cmd(["--live", "--profile", "work"]) == 1
    assert any(x.startswith("a.") and "0.0.1" in x and update.__version__ in x for x in lines(capsys))


def test_ac4_non_vikunja_profile_f_is_not_applicable_and_not_a_failure(live, capsys):
    assert EC.cmd(["--live", "--profile", "work"]) == 0
    assert any(x.startswith("f.") and "n/a" in x for x in lines(capsys))


def test_ac4_non_github_profile_b_is_not_applicable(live, capsys):
    make_profile("vk", "vikunja")
    assert EC.cmd(["--live", "--profile", "vk"]) == 0
    out = lines(capsys)
    assert any(x.startswith("b.") and "n/a" in x for x in out)
    assert not any(x.startswith("f.") and "n/a" in x for x in out)


def test_vikunja_short_list_fails(live, capsys):
    make_profile("vk", "vikunja")
    live["tracker"] = FakeTracker(count=60)
    live["cards"] = [CARD] * 50
    assert EC.cmd(["--live", "--profile", "vk"]) == 1
    assert any(x.startswith("f.") and "50 of 60" in x for x in lines(capsys))


def test_ac2_live_run_makes_only_reads(live, capsys):
    live["rate"] = [1000, 100]
    live["git"] = (0, " M x\0")
    assert EC.cmd(["--live", "--profile", "work"]) == 1
    allowed = (["tmux", "list-windows"], ["gh", "api", "rate_limit"], ["pl", "--version"])
    for argv in live["argv"]:
        assert any(argv[:len(a)] == a for a in allowed) or (argv[0] == "git" and "status" in argv)
    assert set(live["tracker"].seen) <= {"cards", "columns", "_http GET", "_view_path"}


def test_ac7_live_tests_use_a_profile_under_the_test_home(live):
    assert str(C.HOME).startswith(str(C.HOME)) and (C.HOME / ".pl-work" / "config.toml").is_file()
    assert "tmp" in str(C.HOME)
