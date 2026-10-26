"""Plan 41 slice 2: alerts for agents that wait, finish without moving their card, or are blocked on an unmerged card."""
import pytest

from pl import accounts, agent_alerts, agent_screen, agents, alerts, dispatch
from pl import config as C
from pl.trackers import github

SECRET = "SECRET card words"
MIN = 60.0
REAL_CHECK = agent_alerts.check   # conftest fakes it for every other test
PR = "https://github.com/o/r/pull/9"


def _card(cid, col="In progress", worker=None, **meta):
    m = {"pipeline_mode": "auto", **meta}
    if worker is not None:
        m["worker"] = worker
    return {"id": cid, "title": SECRET, "list_id": col, "description": SECRET, "metadata": m}


def _worker(stage="run", **extra):
    return {"stage": stage, "pane": "%1", "window": "run-x", "session_id": "s1", **extra}


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PL_CONFIG_DIR", raising=False)
    d = tmp_path / ".pl-t"
    d.mkdir()
    (d / "config.toml").write_text("")
    C.load("t")
    monkeypatch.setattr(agents, "col_name", lambda lid: lid)
    monkeypatch.setattr(dispatch, "col_name", lambda lid: lid)
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


@pytest.fixture
def clock(monkeypatch):
    t = {"now": 1_000_000.0}
    monkeypatch.setattr(alerts, "_clock", lambda: t["now"])
    monkeypatch.setattr(dispatch.time, "time", lambda: t["now"])
    monkeypatch.setattr(agent_alerts.time, "time", lambda: t["now"])
    return t


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(dispatch, "notify", lambda title, fix="": out.append((title, fix)))
    return out


@pytest.fixture
def screen(monkeypatch):
    s = {"text": "", "word": "working", "why": ""}
    monkeypatch.setattr(agent_screen, "read", lambda pane, lines=20: (s["text"], s["word"], s["why"]))
    return s


def _pass(cards, st=None, restarting=()):
    st = {} if st is None else st
    REAL_CHECK(cards, st, set(restarting))
    return st


def _open_keys():
    return sorted(a["key"] for a in alerts.listing())


def _events(monkeypatch):
    out = []
    monkeypatch.setattr(alerts, "_logged", lambda kind, **d: out.append((kind, d)))
    return out


# ---------- WP1: agent_waiting ----------

def _wait(screen, why="Which database should I use?"):
    screen.update(text="Which database should I use?", word="waiting for you", why=why)


def test_waiting_alert_opens_after_limit_with_window_and_fix(clock, sent, screen):
    _wait(screen)
    c = _card("o/r#7", worker=_worker())
    st = _pass([c])
    assert _open_keys() == []
    clock["now"] += 31 * MIN
    _pass([c], st)
    (a,) = alerts.listing()
    assert a["key"] == "agent_waiting:o/r#7" and a["severity"] == "warn" and a["cards"] == ["o/r#7"]
    assert "r#7" in a["title"] and "run agent waits for you" in a["title"] and "Which database" in a["title"]
    assert "run-x" in a["fix"] and "pl drop 'r#7'" in a["fix"] and "pl retry 'r#7'" in a["fix"]
    assert len(sent) == 1


def test_human_gate_waiting_alert_is_high(clock, sent, screen):
    screen.update(text="GATE: before-push", word="working", why="")
    c = _card("o/r#7", worker=_worker())
    st = _pass([c])
    clock["now"] += 31 * MIN
    _pass([c], st)
    (a,) = alerts.listing()
    assert a["severity"] == "high" and "before-push" in a["title"]


def test_waiting_under_limit_opens_nothing(clock, sent, screen):
    _wait(screen)
    c = _card("o/r#7", worker=_worker())
    st = _pass([c])
    clock["now"] += 29 * MIN
    _pass([c], st)
    assert _open_keys() == [] and sent == []


def test_release_waiting_after_zero_uses_30_minutes(clock, sent, screen, monkeypatch):
    monkeypatch.setitem(C.DISPATCH, "release_waiting_after", 0)
    _wait(screen)
    c = _card("o/r#7", worker=_worker())
    st = _pass([c])
    clock["now"] += 29 * MIN
    _pass([c], st)
    assert _open_keys() == []
    clock["now"] += 2 * MIN
    _pass([c], st)
    assert _open_keys() == ["agent_waiting:o/r#7"]


def test_waiting_skipped_while_permission_wait_alert_open(clock, sent, screen):
    _wait(screen)
    alerts.open("permission_wait:o/r#7", "warn", "agent waiting", "fix")
    c = _card("o/r#7", worker=_worker(permission_wait="Allow Bash?"))
    st = _pass([c])
    clock["now"] += 31 * MIN
    _pass([c], st)
    assert _open_keys() == ["permission_wait:o/r#7"]


def test_waiting_alert_not_duplicated_and_resolves_when_agent_resumes(clock, sent, screen, monkeypatch):
    log = _events(monkeypatch)
    _wait(screen)
    c = _card("o/r#7", worker=_worker())
    st = _pass([c])
    clock["now"] += 31 * MIN
    _pass([c], st)
    clock["now"] += 1 * MIN
    _pass([c], st)
    assert _open_keys() == ["agent_waiting:o/r#7"]
    assert [k for k, _ in log].count("alert_opened") == 1
    screen.update(text="editing files", word="working", why="")
    _pass([c], st)
    assert _open_keys() == []
    assert [k for k, _ in log].count("alert_resolved") == 1
    assert st["agent_waits"] == {}


# ---------- WP2: agent_unmoved ----------

def _finished(screen):
    screen.update(text="$ ", word="finished", why="$")


def test_run_agent_finished_with_pr_not_moved_alerts_move_fix(clock, sent, screen):
    _finished(screen)
    c = _card("o/r#7", worker=_worker(), pr_urls=[PR])
    _pass([c])
    (a,) = alerts.listing()
    assert a["key"] == "agent_unmoved:o/r#7" and a["severity"] == "warn"
    assert "finished with a PR but the card was not moved" in a["title"]
    assert a["fix"] == f'pl move \'r#7\' "PR open" --pr {PR}'


def test_stage_agent_finished_without_change_alerts_retry_fix(clock, sent, screen):
    _finished(screen)
    c = _card("o/r#7", "Spec ready", worker=_worker("plan"))
    _pass([c])
    (a,) = alerts.listing()
    assert a["key"] == "agent_unmoved:o/r#7"
    assert "plan agent finished without moving the card" in a["title"]
    assert "pl retry 'r#7'" in a["fix"] and "pl drop 'r#7'" in a["fix"]


def test_finished_turn_hook_counts_as_finished(clock, sent, screen):
    c = _card("o/r#7", "Spec ready", worker=_worker("plan", finished_turn=True))
    _pass([c])
    assert _open_keys() == ["agent_unmoved:o/r#7"]


def test_unmoved_alert_resolves_when_card_reaches_pr_open(clock, sent, screen):
    _finished(screen)
    c = _card("o/r#7", worker=_worker(), pr_urls=[PR])
    _pass([c])
    assert _open_keys() == ["agent_unmoved:o/r#7"]
    moved = _card("o/r#7", "PR open", worker=_worker(), pr_urls=[PR])
    _pass([moved])
    assert _open_keys() == []


def test_dead_agent_being_restarted_opens_no_alert(clock, sent, screen):
    _finished(screen)
    c = _card("o/r#7", worker=_worker(), pr_urls=[PR])
    _pass([c], restarting={"o/r#7"})
    assert _open_keys() == []


def test_unmoved_repeat_pass_no_duplicate(clock, sent, screen, monkeypatch):
    log = _events(monkeypatch)
    _finished(screen)
    c = _card("o/r#7", worker=_worker(), pr_urls=[PR])
    _pass([c])
    clock["now"] += 5 * MIN
    _pass([c])
    assert _open_keys() == ["agent_unmoved:o/r#7"]
    assert [k for k, _ in log].count("alert_opened") == 1
    assert len(sent) == 1


def test_working_agent_opens_no_unmoved_alert(clock, sent, screen):
    c = _card("o/r#7", worker=_worker())
    _pass([c])
    assert _open_keys() == []


# ---------- WP3: agent_blocked_dep ----------

def test_missing_prerequisite_on_screen_alerts_with_unmerged_card(clock, sent, screen):
    screen.update(text="stopped: prerequisite slice o/r#5 is not merged", word="stopped", why="missing prerequisite")
    dep = _card("o/r#5", "PR open")
    c = _card("o/r#7", "Spec ready", worker=_worker("plan"))
    _pass([c, dep])
    (a,) = alerts.listing()
    assert a["key"] == "agent_blocked_dep:o/r#7" and a["severity"] == "warn"
    assert a["cards"] == ["o/r#7", "o/r#5"]
    assert "r#5" in a["title"] and "merge 'r#5'" in a["fix"] and "pl depends 'r#7' --clear" in a["fix"]


def test_screen_naming_unmerged_slice_alerts(clock, sent, screen):
    screen.update(text="This needs slice 1 merged first", word="working", why="")
    s1 = _card("o/r#5", "PR open", plan=9, slice=1)
    other = _card("o/r#6", "PR open", plan=8, slice=1)
    c = _card("o/r#7", worker=_worker(), plan=9, slice=2)
    _pass([c, s1, other])
    (a,) = alerts.listing()
    assert a["cards"] == ["o/r#7", "o/r#5"]


def test_run_agent_with_open_depends_on_alerts(clock, sent, screen):
    dep = _card("o/r#5", "PR open")
    c = _card("o/r#7", worker=_worker(), depends_on=["o/r#5"])
    _pass([c, dep])
    assert _open_keys() == ["agent_blocked_dep:o/r#7"]


def test_plan_agent_with_open_depends_on_is_not_blocked(clock, sent, screen):
    dep = _card("o/r#5", "PR open")
    c = _card("o/r#7", "Spec ready", worker=_worker("plan"), depends_on=["o/r#5"])
    _pass([c, dep])
    assert _open_keys() == []


def test_screen_naming_merged_card_opens_nothing(clock, sent, screen):
    screen.update(text="prerequisite slice o/r#5 is not merged", word="working", why="")
    dep = _card("o/r#5", "Done")
    c = _card("o/r#7", worker=_worker())
    _pass([c, dep])
    assert _open_keys() == []


def test_screen_card_without_blocking_phrase_opens_nothing(clock, sent, screen):
    screen.update(text="created o/r#5", word="working", why="")
    dep = _card("o/r#5", "PR open")
    c = _card("o/r#7", worker=_worker())
    _pass([c, dep])
    assert _open_keys() == []


def test_blocked_alert_resolves_when_dependency_done(clock, sent, screen):
    dep = _card("o/r#5", "PR open")
    c = _card("o/r#7", worker=_worker(), depends_on=["o/r#5"])
    _pass([c, dep])
    assert _open_keys() == ["agent_blocked_dep:o/r#7"]
    _pass([c, _card("o/r#5", "Done")])
    assert _open_keys() == []


def test_blocked_repeat_pass_no_duplicate(clock, sent, screen, monkeypatch):
    log = _events(monkeypatch)
    dep = _card("o/r#5", "PR open")
    c = _card("o/r#7", worker=_worker(), depends_on=["o/r#5"])
    _pass([c, dep])
    clock["now"] += 5 * MIN
    _pass([c, dep])
    assert _open_keys() == ["agent_blocked_dep:o/r#7"]
    assert [k for k, _ in log].count("alert_opened") == 1


def test_titles_hold_card_ids_not_card_text(clock, sent, screen):
    dep = _card("o/r#5", "PR open")
    c = _card("o/r#7", worker=_worker(), depends_on=["o/r#5"], pr_urls=[PR])
    _pass([c, dep])
    _finished(screen)
    _pass([c, dep])
    assert _open_keys()
    for a in alerts.listing():
        assert "SECRET" not in a["title"] and "SECRET" not in a["fix"]


def test_detectors_make_no_github_call(clock, sent, screen, monkeypatch):
    def boom(args):
        raise AssertionError("github call")

    monkeypatch.setattr(github, "_gh", boom)
    _finished(screen)
    dep = _card("o/r#5", "PR open")
    c = _card("o/r#7", worker=_worker(), depends_on=["o/r#5"], pr_urls=[PR])
    _pass([c, dep])


def test_dropped_card_resolves_its_alerts(clock, sent, screen):
    _finished(screen)
    c = _card("o/r#7", worker=_worker(), pr_urls=[PR])
    _pass([c])
    assert _open_keys() == ["agent_unmoved:o/r#7"]
    _pass([_card("o/r#7", worker=_worker(), pr_urls=[PR], dropped_at="2026-10-26T00:00:00Z")])
    assert _open_keys() == []


def test_dispatch_once_runs_agent_alerts(monkeypatch):
    calls = []
    cs = [{"id": "o/r#1", "title": "t", "list_id": "Inbox", "updated_at": "2026-10-01", "metadata": {"pipeline_mode": "auto"}}]
    monkeypatch.setattr(agent_alerts, "check", lambda cards, st, restarting: calls.append((cards, restarting)))
    monkeypatch.setattr(dispatch, "registry", lambda: {})
    monkeypatch.setattr(dispatch, "cards", lambda: cs)
    monkeypatch.setattr(dispatch, "col_name", lambda lid: lid)
    monkeypatch.setattr(dispatch, "update", lambda *a, **k: None)
    monkeypatch.setattr(dispatch, "start_worker", lambda *a, **k: None)
    monkeypatch.setattr(dispatch, "sweep_untracked", lambda *a, **k: 0)
    monkeypatch.setattr(accounts, "exhausted_profiles", lambda: set())
    for name in ("mirror_to_product", "ensure_services"):
        monkeypatch.setattr(dispatch, name, lambda *a, **k: None)
    dispatch.dispatch_once(1, False, pull=False)
    assert calls == [(cs, {"o/r#1"})]


def test_unreadable_pane_keeps_open_alert_and_notifies_once(clock, sent, screen, monkeypatch):
    _finished(screen)
    c = _card("o/r#7", worker=_worker(), pr_urls=[PR])
    _pass([c])
    assert len(sent) == 1
    monkeypatch.setattr(agent_screen, "read", lambda pane, lines=20: (None, "", ""))
    _pass([c])
    assert _open_keys() == ["agent_unmoved:o/r#7"]
    monkeypatch.setattr(agent_screen, "read", lambda pane, lines=20: (screen["text"], screen["word"], screen["why"]))
    _pass([c])
    assert _open_keys() == ["agent_unmoved:o/r#7"] and len(sent) == 1


def test_unreadable_pane_opens_nothing_new(clock, sent, monkeypatch):
    monkeypatch.setattr(agent_screen, "read", lambda pane, lines=20: (None, "", ""))
    _pass([_card("o/r#7", worker=_worker(), pr_urls=[PR])])
    assert _open_keys() == [] and sent == []


def test_dispatch_pass_survives_a_failing_agent_check(monkeypatch, capsys):
    events_seen = []
    cs = [{"id": "o/r#1", "title": "t", "list_id": "Inbox", "updated_at": "2026-10-01", "metadata": {"pipeline_mode": "auto"}}]

    def boom(*a):
        raise RuntimeError("tmux hiccup")

    monkeypatch.setattr(agent_alerts, "check", boom)
    monkeypatch.setattr(dispatch.events, "emit", lambda kind, **d: events_seen.append((kind, d)))
    monkeypatch.setattr(dispatch, "registry", lambda: {})
    monkeypatch.setattr(dispatch, "cards", lambda: cs)
    monkeypatch.setattr(dispatch, "col_name", lambda lid: lid)
    monkeypatch.setattr(dispatch, "update", lambda *a, **k: None)
    monkeypatch.setattr(dispatch, "start_worker", lambda *a, **k: None)
    seen = []
    monkeypatch.setattr(dispatch, "sweep_untracked", lambda *a, **k: seen.append(1) or 0)
    monkeypatch.setattr(accounts, "exhausted_profiles", lambda: set())
    for name in ("mirror_to_product", "ensure_services"):
        monkeypatch.setattr(dispatch, name, lambda *a, **k: None)
    dispatch.dispatch_once(1, False, pull=False)
    assert seen and any(k == "error" and "tmux hiccup" in d["message"] for k, d in events_seen)
