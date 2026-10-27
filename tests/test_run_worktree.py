"""Run agents start in their card's worktree; the main checkout is never written to."""
import subprocess
from pathlib import Path

import pytest

from pl import config as C
from pl import dispatch, harnesses, worktrees
from pl.util import slug_of

REAL_FOR_RUN = worktrees.for_run   # conftest fakes it for every other test

CARD = {"id": "acme/app#7", "title": "Add thing", "metadata": {}}


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("PL_CONFIG_DIR", raising=False)
    C.load()
    C.WORK_DIR = tmp_path / "repo"
    C.WORK_DIR.mkdir()
    C.WORKTREES_DIR = tmp_path / "wts"
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


class FakeGit:
    def __init__(self, listing="", fail=None, local_branch=False, remote_branch=False):
        self.calls, self.listing, self.fail = [], listing, fail
        self.local_branch, self.remote_branch = local_branch, remote_branch

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        sub = argv[1:]
        rc, out, err = 0, "", ""
        if sub[:2] == ["worktree", "list"]:
            out = self.listing
        elif sub[0] == "show-ref":
            rc = 0 if self.local_branch else 1
        elif sub[0] == "rev-parse":
            rc = 0 if self.remote_branch else 1
        elif self.fail and sub[:2] == self.fail[0]:
            rc, err = 128, self.fail[1]
        return subprocess.CompletedProcess(argv, rc, out, err)

    def adds(self):
        return [a[1:] for a in self.calls if a[1:3] == ["worktree", "add"]]


def _slug():
    return slug_of(CARD)


def _dest():
    return Path(C.WORKTREES_DIR) / f"repo-pl-{_slug()}"


def test_new_card_gets_branch_from_default(monkeypatch):
    git = FakeGit()
    monkeypatch.setattr(worktrees, "_run", git)
    path, err = REAL_FOR_RUN(CARD)
    assert (path, err) == (_dest(), None)
    assert ["fetch", "origin"] in [a[1:] for a in git.calls]
    assert git.adds() == [["worktree", "add", "-b", f"pl/{_slug()}", str(_dest()), "origin/HEAD"]]


def test_existing_worktree_is_reused(monkeypatch):
    listing = f"worktree /elsewhere/old\nHEAD abc\nbranch refs/heads/pl/{_slug()}\n\n"
    git = FakeGit(listing=listing)
    monkeypatch.setattr(worktrees, "_run", git)
    assert REAL_FOR_RUN(CARD) == (Path("/elsewhere/old"), None)
    assert git.adds() == []


def test_existing_local_branch_gets_worktree_without_new_branch(monkeypatch):
    git = FakeGit(local_branch=True)
    monkeypatch.setattr(worktrees, "_run", git)
    assert REAL_FOR_RUN(CARD) == (_dest(), None)
    assert git.adds() == [["worktree", "add", str(_dest()), f"pl/{_slug()}"]]


def test_remote_branch_is_checked_out_as_new_local_branch(monkeypatch):
    git = FakeGit(remote_branch=True)
    monkeypatch.setattr(worktrees, "_run", git)
    REAL_FOR_RUN(CARD)
    assert git.adds() == [["worktree", "add", "-b", f"pl/{_slug()}", str(_dest()), f"origin/pl/{_slug()}"]]


def test_rework_uses_pr_head_branch(monkeypatch):
    card = {**CARD, "metadata": {"rework": {"active": True, "prs": ["https://github.com/acme/app/pull/9"]}}}
    listing = "worktree /wts/other\nHEAD abc\nbranch refs/heads/pl/other\n\n"
    git = FakeGit(listing=listing)
    monkeypatch.setattr(worktrees, "_run", git)
    monkeypatch.setattr(worktrees, "_pr_info", lambda url: {"headRefName": "pl/other"})
    assert REAL_FOR_RUN(card) == (Path("/wts/other"), None)
    assert git.adds() == []


def test_rework_refuses_a_branch_that_looks_like_an_option(monkeypatch):
    card = {**CARD, "metadata": {"rework": {"active": True, "prs": ["u"]}}}
    git = FakeGit()
    monkeypatch.setattr(worktrees, "_run", git)
    monkeypatch.setattr(worktrees, "_pr_info", lambda url: {"headRefName": "--force"})
    REAL_FOR_RUN(card)
    assert git.adds() == [["worktree", "add", "-b", f"pl/{_slug()}", str(_dest()), "origin/HEAD"]]


def test_add_failure_returns_git_last_line(monkeypatch):
    err = f"Preparing\nfatal: '{_dest()}' already exists\n"
    monkeypatch.setattr(worktrees, "_run", FakeGit(fail=(["worktree", "add"], err)))
    assert REAL_FOR_RUN(CARD) == (None, f"fatal: '{_dest()}' already exists")


def test_never_touches_main_checkout_files(monkeypatch):
    allowed = {("worktree", "list"), ("worktree", "add"), ("fetch", "origin"), ("show-ref",), ("rev-parse",)}
    for kw in ({}, {"local_branch": True}, {"remote_branch": True}):
        git = FakeGit(**kw)
        monkeypatch.setattr(worktrees, "_run", git)
        REAL_FOR_RUN(CARD)
        for argv in git.calls:
            assert argv[0] == "git"
            assert any(tuple(argv[1:1 + len(a)]) == a for a in allowed), argv


REAL_GATE = dispatch.untrusted_folder   # conftest fakes it for every other test


@pytest.fixture
def launch(monkeypatch, fake_home):
    """start_worker with tmux, the harness script, the board and for_run faked. Returns what it did."""
    ns = type("Ns", (), {})()
    ns.tmux, ns.prompts, ns.updates, ns.events, ns.notes, ns.for_run = [], [], [], [], [], []
    ns.result = (fake_home / "wts" / "repo-pl-x", None)
    monkeypatch.setattr(C, "PROMPTS", {**C.PROMPTS, "run": C.BUILTIN_PROMPTS["run"], "plan": C.BUILTIN_PROMPTS["plan"]}, raising=False)
    monkeypatch.setattr(C, "CODE_HOST", {}, raising=False)
    monkeypatch.setattr(C, "ATTENTION", None, raising=False)
    monkeypatch.setattr(C, "TMUX_SESSION", "pl-t", raising=False)
    monkeypatch.setattr(C, "STATE_DIR", fake_home / "state", raising=False)
    monkeypatch.setattr(harnesses, "harness_for", lambda stage, c: (harnesses.get("claude"), "a"))
    monkeypatch.setattr(harnesses, "unattended", lambda h: h)
    monkeypatch.setattr(harnesses, "launch_script", lambda h, prof, p, sid, label, **kw: ns.prompts.append(p) or "")
    monkeypatch.setattr(dispatch.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0))
    monkeypatch.setattr(dispatch, "tmux", lambda *a, **k: ns.tmux.append(a) or "%1")
    monkeypatch.setattr(dispatch, "_launch", lambda *a, **k: None)
    monkeypatch.setattr(dispatch, "update", lambda *a, **k: ns.updates.append((a, k)))
    monkeypatch.setattr(dispatch, "notify", lambda *a, **k: ns.notes.append(a))
    monkeypatch.setattr(dispatch.events, "emit", lambda kind, cid=None, **d: ns.events.append((kind, cid, d)))
    monkeypatch.setattr(dispatch.alerts, "open", lambda key, sev, title, hint: ns.events.append(("alert", key, sev)) or "opened")
    monkeypatch.setattr(dispatch.alerts, "resolve", lambda key: None)
    monkeypatch.setattr(dispatch.alerts, "headline", lambda why, t: t)
    monkeypatch.setattr(dispatch.recall, "for_prompt", lambda c, p: "")
    monkeypatch.setattr(dispatch.context, "launch_prompt", lambda c, stage, prompt: prompt)
    monkeypatch.setattr(worktrees, "for_run", lambda c: ns.for_run.append(c["id"]) or ns.result)
    return ns


def _start(stage="run"):
    dispatch.start_worker({"id": "acme/app#7", "title": "Add thing", "metadata": {}}, stage, 0, False)


def test_run_window_starts_in_worktree(launch):
    _start()
    wt = str(launch.result[0])
    window = next(a for a in launch.tmux if a[0] == "new-window")
    assert window[window.index("-c") + 1] == wt
    assert f"worktree={wt}" in launch.prompts[0]


def test_worktree_failure_launches_nothing(launch):
    launch.result = (None, "fatal: x")
    _start()
    assert not [a for a in launch.tmux if a[0] in ("new-window", "new-session")]
    assert launch.updates == []
    errors = [e for e in launch.events if e[0] == "error"]
    assert len(errors) == 1 and "fatal: x" in errors[0][2]["message"]
    assert ("alert", "worktree:acme/app#7", "high") in launch.events
    assert len(launch.notes) == 1


def test_plan_stage_still_starts_in_work_dir(launch):
    _start("plan")
    window = next(a for a in launch.tmux if a[0] == "new-window")
    assert window[window.index("-c") + 1] == str(C.WORK_DIR)
    assert launch.for_run == []


def test_memory_file_written_inside_worktree(launch, monkeypatch):
    (launch.result[0] / ".pl").mkdir(parents=True)
    monkeypatch.setattr(dispatch.recall, "for_prompt", lambda c, p: "remember this")
    _start()
    assert f"memory={launch.result[0] / '.pl' / 'launch'}/" in launch.prompts[0]


def test_trust_gate_checks_the_folder_it_is_given(monkeypatch):
    seen = []
    monkeypatch.setattr(harnesses, "folder_trust", lambda acct, folder: seen.append(folder) or (False,))
    h = harnesses.get("claude")
    assert REAL_GATE(h, "a", Path("/wts")) == Path("/wts")
    assert REAL_GATE(h, "a") == C.WORK_DIR
    assert seen == ["/wts", str(C.WORK_DIR)]
