import pytest

from pl import agents, config as C, dispatch
from pl.agents import deps_waiting, slice_order
from pl.dispatch import approved_label


def _card(cid, title="A card", col="Inbox", **meta):
    return {"id": cid, "title": title, "list_id": col, "metadata": {"pipeline_mode": "auto", **meta}}


@pytest.fixture(autouse=True)
def fake_col_name(monkeypatch):
    monkeypatch.setattr(agents, "col_name", lambda lid: lid)


def test_deps_waiting_names_every_open_dependency():
    dep1 = _card("o/r#1", col="Approved")
    dep2 = _card("o/r#2", col="PR open")
    c = _card("o/r#3", col="Approved", depends_on=["o/r#1", "o/r#2"])
    assert deps_waiting(c, [c, dep1, dep2]) == "waiting: needs r#1, r#2 merged"


def test_a_dependency_in_done_is_met():
    dep1 = _card("o/r#1", col="Done")
    c = _card("o/r#2", col="Approved", depends_on=["o/r#1"])
    assert deps_waiting(c, [c, dep1]) is None


def test_a_dropped_or_missing_dependency_is_never_met():
    dep1 = _card("o/r#1", col="Done", dropped_at="2026-10-23T12:00:00Z")
    c = _card("o/r#2", col="Approved", depends_on=["o/r#1", "o/r#99"])
    assert deps_waiting(c, [c, dep1]) == "waiting: needs r#1, r#99 merged"


def test_slice_order_reads_plan_and_slice_numbers():
    assert slice_order({"title": "Plan 3 slice 2: x"}) == (0, 3, 2)
    assert slice_order({"title": "Plan 10 slice 1:"}) > slice_order({"title": "Plan 3 slice 9:"})
    assert slice_order({"title": "Card 1"}) == (1, 0, 0)


# ---------- WP2: dispatcher wait and slice order ----------


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PL_CONFIG_DIR", raising=False)
    d = tmp_path / ".pl-t"
    d.mkdir()
    (d / "config.toml").write_text("")
    C.load("t")
    C.PROFILES = {"claude": tmp_path / ".claude"}
    C.ACCOUNTS = {"claude": {"harness": "claude"}}
    C.STAGES = {"run": {"account": "claude"}}
    C.PROMPTS = {**C.PROMPTS, "run": "/pl-run {id}"}
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


def _pass_dispatch(monkeypatch, cs, max_runs=1):
    started = []
    monkeypatch.setattr(dispatch, "registry", lambda: {})
    monkeypatch.setattr(dispatch, "cards", lambda: cs)
    monkeypatch.setattr(dispatch, "col_name", lambda lid: lid)
    monkeypatch.setattr(dispatch, "update", lambda cid, **f: None)
    monkeypatch.setattr(dispatch, "start_worker", lambda c, stage, attempts, dry: started.append(c))
    for name in ("mirror_to_product", "ensure_services", "sweep_untracked"):
        monkeypatch.setattr(dispatch, name, lambda *a, **k: None)
    dispatch.dispatch_once(max_runs, False, pull=False)
    return started


def test_a_dependent_approved_card_waits_and_its_dependency_starts(monkeypatch, capsys):
    r36 = _card("o/r#36", title="Plan 22 slice 1: x", col="Approved", profile="claude")
    r37 = _card("o/r#37", title="Plan 22 slice 2: y", col="Approved", profile="claude", depends_on=["o/r#36"])
    started = _pass_dispatch(monkeypatch, [r36, r37], max_runs=1)
    assert [c["id"] for c in started] == ["o/r#36"]
    assert "r#37  run waiting: needs r#36 merged" in capsys.readouterr().out


def test_the_dependent_starts_once_its_dependency_is_done(monkeypatch):
    r36 = _card("o/r#36", title="Plan 22 slice 1: x", col="Done", profile="claude")
    r37 = _card("o/r#37", title="Plan 22 slice 2: y", col="Approved", profile="claude", depends_on=["o/r#36"])
    started = _pass_dispatch(monkeypatch, [r36, r37], max_runs=1)
    assert [c["id"] for c in started] == ["o/r#37"]


def test_startable_cards_start_in_plan_then_slice_order(monkeypatch):
    monkeypatch.setattr(dispatch, "LIVE_RUN_CAP", 10)
    c_p2s1 = _card("o/r#1", title="Plan 2 slice 1: b", col="Approved", updated_at="2026-10-04", profile="claude")
    c_p1s2 = _card("o/r#2", title="Plan 1 slice 2: a2", col="Approved", updated_at="2026-10-03", profile="claude")
    c_p1s1 = _card("o/r#3", title="Plan 1 slice 1: a1", col="Approved", updated_at="2026-10-02", profile="claude")
    c_other = _card("o/r#4", title="Other", col="Approved", updated_at="2026-10-01", profile="claude")
    started = _pass_dispatch(monkeypatch, [c_p2s1, c_p1s2, c_p1s1, c_other], max_runs=10)
    assert [c["id"] for c in started] == ["o/r#3", "o/r#2", "o/r#1", "o/r#4"]


def test_pl_list_shows_the_dependency_wait():
    r36 = _card("o/r#36", col="Approved")
    r37 = _card("o/r#37", col="Approved", depends_on=["o/r#36"])
    assert approved_label(r37, "Approved", {}, [r36, r37]) == "waiting: needs r#36 merged"

