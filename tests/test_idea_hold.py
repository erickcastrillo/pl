from types import SimpleNamespace

import pytest

from pl import cli, commands, dispatch, trackers
from pl import config as C
from test_local_board_flow import _write_profile, fake_home  # noqa: F401


@pytest.fixture
def started(fake_home, monkeypatch):  # noqa: F811
    _write_profile(fake_home, "p", '[tracker]\ntype = "local"\n')
    C.load("p")
    trackers.reset()
    log = []
    monkeypatch.setattr(dispatch, "start_worker", lambda c, stage, attempts, dry: log.append((c["id"], stage)))
    for name in ("sweep_untracked", "ensure_services", "permission_wait", "trust_wait"):
        monkeypatch.setattr(dispatch, name, lambda *a, **k: None)
    monkeypatch.setattr(dispatch, "notify", lambda *a, **k: None)
    monkeypatch.setattr(commands, "notify", lambda *a, **k: None)
    monkeypatch.setattr(dispatch, "registry", lambda: {})
    monkeypatch.setattr(dispatch, "healthy_profile", lambda want, cards: want or "p")
    monkeypatch.setattr(commands, "intake_configured", lambda: False)
    C.PROFILES = {"p": fake_home / ".claude-p"}
    C.ACCOUNTS = {"p": {"harness": "claude", "config_dir": str(fake_home / ".claude-p")}}
    C.PROMPTS = {"spec": "/spec {id}", "plan": "/plan {id}", "run": "/run {id}"}
    C.USER_EMAIL = "octo@example.com"
    return log


def idea(**kw):
    return SimpleNamespace(**{"text": "Add export", "title": None, "doc": [], "account": None, "repo": [], "start": False, **kw})


def run_cli(monkeypatch, *argv):
    monkeypatch.setattr("sys.argv", ["pl", "--profile", "p", *argv])
    cli.main()


def only_card():
    trackers.reset("tracker")
    (c,) = trackers.get("tracker").cards()
    return c


def passes(n=3):
    for _ in range(n):
        dispatch.dispatch_once(1, dry=False, pull=False)
        trackers.reset("tracker")


def test_hold_files_a_parked_inbox_card_and_names_unhold(started, capsys):
    commands.cmd_idea(idea(hold=True))
    c = only_card()
    assert "parked" in c["tags"] and commands.col_name(c["list_id"]) == "Inbox"
    out = capsys.readouterr().out
    assert f"pl unhold {c['id']}" in out and "dispatcher writes its spec" not in out


def test_a_held_card_gets_no_agent_over_many_passes(started):
    commands.cmd_idea(idea(hold=True))
    passes(4)
    assert started == [] and "worker" not in (only_card().get("metadata") or {})


def test_a_pass_during_filing_starts_nothing(started, monkeypatch):
    t = trackers.get("tracker")
    created, real_create, real_update = [], t.create, t.update
    monkeypatch.setattr(t, "create", lambda col, **kw: created.append(kw) or real_create(col, **kw))

    def update(item_id, **kw):
        dispatch.dispatch_once(1, dry=False, pull=False)
        return real_update(item_id, **kw)

    monkeypatch.setattr(t, "update", update)
    commands.cmd_idea(idea(hold=True))
    assert started == [] and "pipeline_mode" not in created[0]["metadata"]
    assert only_card()["metadata"]["pipeline_mode"] == "auto"


def test_unhold_starts_the_spec_on_the_next_pass(started):
    commands.cmd_idea(idea(hold=True))
    passes(2)
    commands.unhold(only_card()["id"])
    passes(1)
    assert [s for _, s in started] == ["spec"]


def test_a_plain_idea_still_starts(started, capsys):
    commands.cmd_idea(idea())
    assert "parked" not in only_card()["tags"]
    assert "the dispatcher writes its spec on its next pass" in capsys.readouterr().out
    passes(1)
    assert [s for _, s in started] == ["spec"]


def test_hold_with_an_intake_board_waits_in_triage_untagged(started, monkeypatch, capsys):
    made = []

    class Intake:
        def create(self, column, **kw):
            made.append((column, kw))
            return {"id": "abcdef12-0000"}

        def url(self, _id):
            return "u"

    monkeypatch.setattr(commands, "intake_configured", lambda: True)
    monkeypatch.setattr(commands, "product_lists", lambda: ["Triage"])
    monkeypatch.setattr(trackers, "get", lambda kind: Intake())
    commands.cmd_idea(idea(hold=True))
    (col, kw), = made
    assert col == "Triage" and kw["assigned_to"] is None and "parked" not in kw["tags"]
    assert "waiting to be assessed" in capsys.readouterr().out


@pytest.mark.parametrize("intake", [False, True])
def test_hold_and_start_conflict_and_create_nothing(started, monkeypatch, capsys, intake):
    monkeypatch.setattr(commands, "intake_configured", lambda: intake)
    with pytest.raises(SystemExit) as e:
        run_cli(monkeypatch, "idea", "x", "--hold", "--start")
    assert e.value.code == 2 and "conflict" in capsys.readouterr().err
    trackers.reset("tracker")
    assert trackers.get("tracker").cards() == []


def test_a_tag_that_will_not_stick_leaves_the_card_out_of_the_pipeline(started, monkeypatch):
    t = trackers.get("tracker")
    real_card = t.card
    monkeypatch.setattr(t, "card", lambda i: {**real_card(i), "tags": []})
    with pytest.raises(SystemExit):
        commands.cmd_idea(idea(hold=True))
    assert "pipeline_mode" not in (only_card().get("metadata") or {})


def test_a_read_only_loop_idea_gets_no_agent_until_unhold(started, monkeypatch):
    monkeypatch.setenv("PL_LOOP_WRITES", "0")
    monkeypatch.setenv("PL_LOOP", "invoices")
    commands.cmd_idea(idea())
    assert {"proposed", "loop:invoices", "parked"} <= set(only_card()["tags"])
    monkeypatch.delenv("PL_LOOP_WRITES")
    passes(2)
    assert started == []
    commands.unhold(only_card()["id"])
    passes(1)
    assert [s for _, s in started] == ["spec"]


def test_interview_refuses_hold(started, monkeypatch, capsys):
    from pl import interview
    monkeypatch.setattr(interview, "_need_terminal", lambda *a: pytest.fail("terminal checked first"))
    with pytest.raises(SystemExit) as e:
        run_cli(monkeypatch, "idea", "--interview", "x", "--hold")
    err = capsys.readouterr().err
    assert e.value.code == 2 and "--hold" in err and "does not apply" in err


class UrlRaises:
    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def url(self, item_id):
        if item_id == "dry-run":
            raise ValueError("not a task id")
        return self._inner.url(item_id)


@pytest.mark.parametrize("intake", [False, True])
def test_dry_run_prints_the_card_without_a_traceback(started, monkeypatch, capsys, intake):
    monkeypatch.setattr(commands, "intake_configured", lambda: intake)
    monkeypatch.setattr(commands, "product_lists", lambda: ["Triage"])
    real = trackers.get
    monkeypatch.setattr(trackers, "get", lambda kind: UrlRaises(real("tracker")))
    trackers.set_dry_run()
    try:
        commands.cmd_idea(idea())
    finally:
        trackers.set_dry_run(False)
    assert "would create" in capsys.readouterr().out


def test_dry_run_with_hold_shows_the_parked_tag(started, capsys):
    trackers.set_dry_run()
    try:
        commands.cmd_idea(idea(hold=True))
    finally:
        trackers.set_dry_run(False)
    assert "would create" in (out := capsys.readouterr().out) and "parked" in out
    assert trackers.get("tracker").cards() == []


def test_help_states_both_profiles_and_hold(started, monkeypatch, capsys):
    for argv in (["help"], ["idea", "--help"]):
        try:
            run_cli(monkeypatch, *argv)
        except SystemExit:
            pass
        out = capsys.readouterr().out
        for word in ("Triage", "Inbox", "next pass", "--hold", "pl unhold"):
            assert word in out, (argv, word)
