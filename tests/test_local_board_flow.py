import argparse
from types import SimpleNamespace
import pytest

from pl import config as C
from pl import commands, dispatch, trackers
from test_tracker_github import FakeGh


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("PL_CONFIG_DIR", "PL_TMUX_SESSION"):
        monkeypatch.delenv(var, raising=False)
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


def _write_profile(home, name, toml_text):
    d = home / f".pl-{name}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.toml").write_text(toml_text)
    return d


def _run_flow(tmp_path, monkeypatch, is_local=True):
    tmp_path.mkdir(parents=True, exist_ok=True)
    stages_recorded = []
    spec_file = tmp_path / "spec.md"
    spec_file.write_text("## Problem\nExport missing\n\n## Outcome\nExport added\n")
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("## Summary\nPlan 1\n\n## Minimum change\n1 file\n")

    def fake_start_worker(c, stage, attempts, dry):
        stages_recorded.append((c["id"], stage))
        if stage == "spec":
            commands.cmd_section(argparse.Namespace(id=c["id"], name="SPEC", from_=str(spec_file), force=False))
            commands.move_to(c["id"], "Spec ready")
        elif stage == "plan":
            commands.cmd_section(argparse.Namespace(id=c["id"], name="PLAN", from_=str(plan_file), force=False))
            commands.move_to(c["id"], "Plan for review")
        elif stage == "run":
            pass

    monkeypatch.setattr(dispatch, "start_worker", fake_start_worker)
    monkeypatch.setattr(dispatch, "notify", lambda *a, **k: None)
    monkeypatch.setattr(commands, "notify", lambda *a, **k: None)
    monkeypatch.setattr(dispatch, "sweep_untracked", lambda *a, **k: 0)
    monkeypatch.setattr(dispatch, "ensure_services", lambda *a, **k: None)
    monkeypatch.setattr(dispatch, "registry", lambda: {})
    monkeypatch.setattr(dispatch, "healthy_profile", lambda want, cards: want or "acme")
    monkeypatch.setattr(dispatch, "permission_wait", lambda *a, **k: None)
    monkeypatch.setattr(dispatch, "trust_wait", lambda *a, **k: None)

    C.PROFILES = {"acme": tmp_path / ".claude-acme"}
    C.ACCOUNTS = {"acme": {"harness": "claude", "config_dir": str(tmp_path / ".claude-acme")}}
    C.PROMPTS = {
        "spec": "/spec-writer {id}",
        "design": "/ui-designer {id}",
        "plan": "/plan-writer {id}",
        "run": "/loop 5m /run-plan {id}",
    }
    C.USER_EMAIL = "octo@example.com"
    C.USER_LOGIN = "octo"

    # Step 1: pl idea with --start
    commands.cmd_idea(
        SimpleNamespace(
            text="Add export\n\nExport data as CSV.",
            title="Add export",
            doc=[],
            account=None,
            repo=[],
            start=True,
        )
    )

    t = trackers.get("tracker")
    all_cards = t.cards()
    assert len(all_cards) == 1
    cid = all_cards[0]["id"]

    # Step 2: dispatch_once starts spec, then agent moves to Spec ready
    dispatch.dispatch_once(1, dry=False, pull=False)
    trackers.reset("tracker")
    t = trackers.get("tracker")
    assert t.card(cid)["list_id"] == t.columns()["Spec ready"]

    # Step 3: dispatch_once starts plan, then agent moves to Plan for review
    dispatch.dispatch_once(1, dry=False, pull=False)
    trackers.reset("tracker")
    t = trackers.get("tracker")
    assert t.card(cid)["list_id"] == t.columns()["Plan for review"]

    # Step 4: cmd_approve moves to Approved
    commands.cmd_approve(argparse.Namespace(id=cid, force=False))
    trackers.reset("tracker")
    t = trackers.get("tracker")
    assert t.card(cid)["list_id"] == t.columns()["Approved"]

    # Step 5: dispatch_once starts run
    dispatch.dispatch_once(1, dry=False, pull=False)

    # Step 6: move_to PR open with --pr
    pr_url = "https://github.com/your-org/your-repo/pull/42"
    commands.cmd_move(argparse.Namespace(id=cid, column="PR open", pr=pr_url))
    trackers.reset("tracker")
    t = trackers.get("tracker")
    assert t.card(cid)["list_id"] == t.columns()["PR open"]

    # Step 7: commands.done
    commands.cmd_done(argparse.Namespace(id=cid, note=None))
    trackers.reset("tracker")
    t = trackers.get("tracker")
    assert t.card(cid)["list_id"] == t.columns()["Done"]

    final_card = t.card(cid)
    return stages_recorded, final_card


def test_engineering_card_runs_every_stage_on_the_local_board(fake_home, monkeypatch):
    # 1. Run flow against local board
    _write_profile(fake_home, "localprof", '[tracker]\ntype = "local"\n')
    C.load("localprof")
    trackers.reset()

    local_stages, local_card = _run_flow(fake_home / "local_run", monkeypatch, is_local=True)

    # 2. Run flow against fake GitHub tracker fixture
    from pl.trackers import github
    gh_options = [{"id": f"opt-{c.lower().replace(' ', '-')}", "name": c} for c in C.COLUMNS]
    gh_fake = FakeGh(options=gh_options)
    monkeypatch.setattr(github, "_gh", gh_fake)
    _write_profile(fake_home, "ghprof", '[tracker]\ntype = "github-project"\nowner = "acme"\nnumber = 7\nrepo = "acme/app"\n')
    C.load("ghprof")
    trackers.reset()

    gh_stages, gh_card = _run_flow(fake_home / "gh_run", monkeypatch, is_local=False)

    # 3. Assert stage sequence
    assert [stage for _, stage in local_stages] == ["spec", "plan", "run"]
    assert [stage for _, stage in gh_stages] == ["spec", "plan", "run"]
    assert [stage for _, stage in local_stages] == [stage for _, stage in gh_stages]

    # 4. Assert metadata keys match
    for k in ("worker", "approved_at", "pr_urls"):
        assert k in local_card["metadata"], f"{k} missing in local card metadata"
        assert k in gh_card["metadata"], f"{k} missing in github card metadata"

    assert local_card["metadata"]["worker"] is None
    assert gh_card["metadata"]["worker"] is None
    assert local_card["metadata"]["pr_urls"] == gh_card["metadata"]["pr_urls"] == ["https://github.com/your-org/your-repo/pull/42"]
    assert bool(local_card["metadata"]["approved_at"])
    assert bool(gh_card["metadata"]["approved_at"])
