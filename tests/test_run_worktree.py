"""Run agents start in their card's worktree; the main checkout is never written to."""
import subprocess
from pathlib import Path

import pytest

from pl import config as C
from pl import worktrees
from pl.util import slug_of

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
    path, err = worktrees.for_run(CARD)
    assert (path, err) == (_dest(), None)
    assert ["fetch", "origin"] in [a[1:] for a in git.calls]
    assert git.adds() == [["worktree", "add", "-b", f"pl/{_slug()}", str(_dest()), "origin/HEAD"]]


def test_existing_worktree_is_reused(monkeypatch):
    listing = f"worktree /elsewhere/old\nHEAD abc\nbranch refs/heads/pl/{_slug()}\n\n"
    git = FakeGit(listing=listing)
    monkeypatch.setattr(worktrees, "_run", git)
    assert worktrees.for_run(CARD) == (Path("/elsewhere/old"), None)
    assert git.adds() == []


def test_existing_local_branch_gets_worktree_without_new_branch(monkeypatch):
    git = FakeGit(local_branch=True)
    monkeypatch.setattr(worktrees, "_run", git)
    assert worktrees.for_run(CARD) == (_dest(), None)
    assert git.adds() == [["worktree", "add", str(_dest()), f"pl/{_slug()}"]]


def test_remote_branch_is_checked_out_as_new_local_branch(monkeypatch):
    git = FakeGit(remote_branch=True)
    monkeypatch.setattr(worktrees, "_run", git)
    worktrees.for_run(CARD)
    assert git.adds() == [["worktree", "add", "-b", f"pl/{_slug()}", str(_dest()), f"origin/pl/{_slug()}"]]


def test_rework_uses_pr_head_branch(monkeypatch):
    card = {**CARD, "metadata": {"rework": {"active": True, "prs": ["https://github.com/acme/app/pull/9"]}}}
    listing = "worktree /wts/other\nHEAD abc\nbranch refs/heads/pl/other\n\n"
    git = FakeGit(listing=listing)
    monkeypatch.setattr(worktrees, "_run", git)
    monkeypatch.setattr(worktrees, "_pr_info", lambda url: {"headRefName": "pl/other"})
    assert worktrees.for_run(card) == (Path("/wts/other"), None)
    assert git.adds() == []


def test_rework_refuses_a_branch_that_looks_like_an_option(monkeypatch):
    card = {**CARD, "metadata": {"rework": {"active": True, "prs": ["u"]}}}
    git = FakeGit()
    monkeypatch.setattr(worktrees, "_run", git)
    monkeypatch.setattr(worktrees, "_pr_info", lambda url: {"headRefName": "--force"})
    worktrees.for_run(card)
    assert git.adds() == [["worktree", "add", "-b", f"pl/{_slug()}", str(_dest()), "origin/HEAD"]]


def test_add_failure_returns_git_last_line(monkeypatch):
    err = f"Preparing\nfatal: '{_dest()}' already exists\n"
    monkeypatch.setattr(worktrees, "_run", FakeGit(fail=(["worktree", "add"], err)))
    assert worktrees.for_run(CARD) == (None, f"fatal: '{_dest()}' already exists")


def test_never_touches_main_checkout_files(monkeypatch):
    allowed = {("worktree", "list"), ("worktree", "add"), ("fetch", "origin"), ("show-ref",), ("rev-parse",)}
    for kw in ({}, {"local_branch": True}, {"remote_branch": True}):
        git = FakeGit(**kw)
        monkeypatch.setattr(worktrees, "_run", git)
        worktrees.for_run(CARD)
        for argv in git.calls:
            assert argv[0] == "git"
            assert any(tuple(argv[1:1 + len(a)]) == a for a in allowed), argv
