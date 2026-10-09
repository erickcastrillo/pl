"""[stages.<stage>] accounts: a stage spreads its agents over a pool of accounts, which may run different harnesses."""
import pytest
import tomlkit

from pl import accounts, dispatch, harnesses, move_agent
from pl import config as C


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PL_CONFIG_DIR", raising=False)
    d = tmp_path / ".pl-t"
    d.mkdir()
    (d / "config.toml").write_text("")
    C.load("t")
    C.PROFILES = {"claude": tmp_path / ".claude", "agy": tmp_path / "agy", "codex": tmp_path / ".codex",
                  "claude2": tmp_path / ".claude2"}
    C.ACCOUNTS = {"claude": {"harness": "claude"}, "agy": {"harness": "antigravity"}, "codex": {"harness": "codex"},
                  "claude2": {"harness": "claude"}}
    C.STAGES = {"run": {"accounts": ["claude", "agy"]}}
    C.PROMPTS = {**C.PROMPTS, "run": "/pl-run {id}"}
    monkeypatch.setattr(accounts, "lists", lambda: {})
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


def _card(n, **meta):
    return {"id": f"o/r#{n}", "title": f"Card {n}", "list_id": "Approved", "updated_at": f"2026-10-0{n}",
            "metadata": {"pipeline_mode": "auto", **meta}}


def _pass(monkeypatch, cs, parked=(), max_runs=2):
    """One dispatcher pass, board and tmux faked. Returns (started (card id, account), board writes)."""
    started, writes = [], []

    def update(cid, **f):
        writes.append((cid, f))
        c = next(x for x in cs if x["id"] == cid)
        c["metadata"] = {k: v for k, v in {**c["metadata"], **f.get("metadata", {})}.items() if v is not None}
    monkeypatch.setattr(dispatch, "registry", lambda: {})
    monkeypatch.setattr(dispatch, "cards", lambda: cs)
    monkeypatch.setattr(dispatch, "col_name", lambda lid: lid)
    monkeypatch.setattr(dispatch, "update", update)
    monkeypatch.setattr(dispatch, "start_worker",
                        lambda c, stage, attempts, dry: started.append((c["id"], harnesses.harness_for(stage, c)[1])))
    monkeypatch.setattr(dispatch, "sweep_untracked", lambda *a, **k: 0)
    monkeypatch.setattr(accounts, "exhausted_profiles", lambda: set(parked))
    monkeypatch.setattr(accounts, "check_parked", lambda: None)
    for name in ("mirror_to_product", "ensure_services"):
        monkeypatch.setattr(dispatch, name, lambda *a, **k: None)
    dispatch.dispatch_once(max_runs, False, pull=False)
    return started, writes


# ---------- config ----------

def _errs(stage_toml):
    doc = tomlkit.parse('[accounts.claude]\nconfig_dir = "~"\n[accounts.agy]\nconfig_dir = "~"\nharness = "antigravity"\n'
                        + stage_toml)
    return [e for e in C.validate(doc) if e.startswith("stages.")]


@pytest.mark.parametrize("toml, want", [
    ('[stages.run]\naccounts = ["claude", "agy"]\n', None),
    ('[stages.run]\naccounts = "claude"\n', "must be a list of account names"),
    ('[stages.run]\naccounts = [1]\n', "must be a list of account names"),
    ('[stages.run]\naccounts = []\n', "must be a list of account names"),
    ('[stages.run]\naccounts = ["claude", "nope"]\n', "'nope'"),
    ('[stages.run]\naccount = "claude"\naccounts = ["claude", "agy"]\n', "remove account"),
    ('[stages.run]\nharness = "claude"\naccounts = ["claude", "agy"]\n', "agy"),
    ('[stages.run]\nharness = "claude"\naccounts = ["claude"]\n', None),
    ('[stages.run]\nharness = "claude"\naccount = "agy"\n',
     "stages.run: harness 'claude' differs from account agy's harness 'antigravity'; remove the stage's harness line"),
    ('[stages.run]\nharness = "antigravity"\naccount = "agy"\n', None),
    ('[stages.run]\nharness = "antigravity"\n', None),               # agy runs antigravity
    ('[stages.run]\nharness = "codex"\n', "no account uses harness 'codex'"),
])
def test_stage_accounts_are_validated(toml, want):
    errs = _errs(toml)
    if want is None:
        assert errs == []
    else:
        assert len(errs) == 1 and want in errs[0], errs


# ---------- harness_for ----------

def test_harness_for_a_pool_takes_the_cards_account_when_it_is_in_the_pool_else_the_first():
    assert harnesses.harness_for("run") == (harnesses.get("claude"), "claude")                  # settings: no card
    h, acct = harnesses.harness_for("run", {"metadata": {"profile": "agy"}})
    assert (h.name, acct) == ("antigravity", "agy")
    assert harnesses.harness_for("run", {"metadata": {"profile": "codex"}})[1] == "claude"       # outside the pool


def test_harness_for_refuses_an_unknown_pool_account():
    C.STAGES = {"run": {"accounts": ["claude", "nope"]}}
    with pytest.raises(SystemExit, match="nope"):
        harnesses.harness_for("run")


def test_healthy_profile_keeps_to_the_pool(monkeypatch):
    monkeypatch.setattr(accounts, "exhausted_profiles", lambda: {"claude"})
    assert accounts.healthy_profile("codex", [], pool=["claude", "agy"]) == "agy"
    assert accounts.healthy_profile(None, [], pool=["claude"]) is None


# ---------- dispatch ----------

def test_two_cards_in_one_pass_go_one_to_each_pool_account(monkeypatch):
    cs = [_card(1, profile="claude"), _card(2, profile="claude")]
    started, writes = _pass(monkeypatch, cs)
    assert started == [("o/r#1", "agy"), ("o/r#2", "claude")]   # card 2's claude counted against card 1's pick
    assert writes == [("o/r#1", {"metadata": {"profile": "agy"}})]


def test_the_least_loaded_pool_account_wins_and_the_cards_own_account_is_ignored(monkeypatch):
    busy = _card(3, profile="claude", worker={"stage": "spec"})   # an auto card on claude: it counts as load
    monkeypatch.setattr(dispatch, "stage_for", lambda c, col: "run" if c["id"] == "o/r#1" else None)
    started, _ = _pass(monkeypatch, [_card(1, profile="codex"), busy])
    assert started == [("o/r#1", "agy")]


def test_a_pool_account_that_is_parked_is_skipped(monkeypatch):
    started, _ = _pass(monkeypatch, [_card(1), _card(2)], parked={"agy"})
    assert started == [("o/r#1", "claude"), ("o/r#2", "claude")]


def test_a_stage_whose_whole_pool_is_parked_waits_and_never_leaves_the_pool(monkeypatch, capsys):
    started, writes = _pass(monkeypatch, [_card(1, profile="codex")], parked={"claude", "agy"})
    assert started == [] and writes == []
    assert "r#1  run waits: its accounts claude, agy are parked (pl accounts)" in capsys.readouterr().out


def test_a_live_agent_keeps_its_account(monkeypatch):
    c = _card(1, profile="agy", worker={"stage": "run", "profile": "agy", "harness": "antigravity", "pane": "%1",
                                        "session_id": "s", "started_at": "2026-10-07T00:00:00+00:00"})
    monkeypatch.setattr(dispatch, "worker_status", lambda w, reg: ("alive", "s"))
    monkeypatch.setattr(dispatch, "screen_hit_limit", lambda *a: None)
    monkeypatch.setattr(dispatch, "run_waiting", lambda w, reg: None)
    monkeypatch.setattr(dispatch, "api_error_wait", lambda w, reg: None)
    for name in ("trust_wait", "permission_wait"):
        monkeypatch.setattr(dispatch, name, lambda *a: None)
    started, writes = _pass(monkeypatch, [c, _card(2, profile="claude")])
    assert started == [("o/r#2", "claude")] and c["metadata"]["worker"]["profile"] == "agy"


@pytest.mark.parametrize("stage", ["run", "plan"])
def test_a_pooled_agent_at_its_limit_restarts_inside_the_pool_on_any_harness(monkeypatch, stage):
    C.STAGES = {stage: {"accounts": ["claude", "agy"]}}
    monkeypatch.setattr(dispatch, "stage_for", lambda c, col: stage)
    c = _card(1, profile="claude", worker={"stage": stage, "profile": "claude", "harness": "claude", "pane": "%1",
                                           "window": "@1", "session_id": "s", "started_at": "2020-01-01T00:00:00+00:00"})
    parked, moves = set(), []
    monkeypatch.setattr(dispatch, "worker_status", lambda w, reg: ("alive", "s"))
    monkeypatch.setattr(dispatch, "screen_hit_limit", lambda *a: "You've hit your limit")
    monkeypatch.setattr(dispatch, "mark_exhausted", lambda prof, screen: parked.add(prof) or "2026-10-07T12:00:00+00:00")
    monkeypatch.setattr(dispatch, "tmux", lambda *a, **k: "")
    monkeypatch.setattr(dispatch, "notify", lambda *a, **k: None)
    monkeypatch.setattr(move_agent, "lock", lambda cid: True)
    monkeypatch.setattr(move_agent, "unlock", lambda cid: None)
    monkeypatch.setattr(move_agent, "move", lambda *a, **k: moves.append(a) or (True, None, True))
    started, writes = _pass(monkeypatch, [c], parked=parked)
    assert moves == []                                   # claude2 is free but outside the pool: the session is not moved there
    assert c["metadata"]["profile"] == "agy" and started == [("o/r#1", "agy")]


def test_two_new_cards_with_no_account_also_spread(monkeypatch):
    started, _ = _pass(monkeypatch, [_card(1), _card(2)])
    assert started == [("o/r#1", "claude"), ("o/r#2", "agy")]
