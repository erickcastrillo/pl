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
        self.calls, self.kwargs, self.listing, self.fail, self.hang = [], [], listing, fail, None
        self.local_branch, self.remote_branch = local_branch, remote_branch

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        self.kwargs.append(kw)
        if self.hang and argv[1:3] == self.hang:
            raise subprocess.TimeoutExpired(argv, kw.get("timeout"))
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


def test_folder_of_repos_starts_in_work_dir_without_git(monkeypatch):
    # a work dir that is not a repo but holds repos: the run skill makes its own worktrees
    (Path(C.WORK_DIR) / "api" / ".git").mkdir(parents=True)
    git = FakeGit()
    monkeypatch.setattr(worktrees, "_run", git)
    assert REAL_FOR_RUN(CARD) == (Path(C.WORK_DIR), None)
    assert git.calls == []


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


def test_git_calls_have_a_timeout_and_never_prompt(monkeypatch):
    git = FakeGit()
    monkeypatch.setattr(worktrees, "_run", git)
    REAL_FOR_RUN(CARD)
    mutating = [kw for argv, kw in zip(git.calls, git.kwargs) if argv[1] in ("fetch", "worktree") and argv[2] != "list"]
    assert len(mutating) == 3
    for kw in mutating:
        assert kw["timeout"] > 0 and kw["env"]["GIT_TERMINAL_PROMPT"] == "0"


@pytest.mark.parametrize("hang", [["fetch", "origin"], ["worktree", "add"]])
def test_a_git_timeout_is_a_failed_worktree(monkeypatch, hang):
    git = FakeGit()
    git.hang = hang
    monkeypatch.setattr(worktrees, "_run", git)
    path, err = REAL_FOR_RUN(CARD)
    assert path is None and f"git {hang[0]}" in err and "timed out" in err


def test_never_touches_main_checkout_files(monkeypatch):
    allowed = {("worktree", "list"), ("worktree", "add"), ("worktree", "prune"), ("fetch", "origin"), ("show-ref",), ("rev-parse",)}
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
    assert len(launch.updates) == 1
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


def _stale_siblings():
    return sorted(p for p in _dest().parent.iterdir() if ".stale-" in p.name)


def test_plain_folder_at_worktree_path_is_set_aside(monkeypatch):
    git = FakeGit()
    monkeypatch.setattr(worktrees, "_run", git)
    _dest().mkdir(parents=True)
    (_dest() / "leftover.txt").write_text("keep")
    assert REAL_FOR_RUN(CARD) == (_dest(), None)
    [moved] = _stale_siblings()
    assert (moved / "leftover.txt").read_text() == "keep"
    assert len(git.adds()) == 1 and str(_dest()) in git.adds()[0]


def test_second_set_aside_gets_a_new_name(monkeypatch):
    monkeypatch.setattr(worktrees, "_run", FakeGit())
    monkeypatch.setattr(worktrees, "_stamp", lambda: "20260101-000000")
    taken = _dest().with_name(_dest().name + ".stale-20260101-000000")
    taken.mkdir(parents=True)
    (taken / "old.txt").write_text("old")
    _dest().mkdir()
    (_dest() / "new.txt").write_text("new")
    assert REAL_FOR_RUN(CARD) == (_dest(), None)
    assert (taken / "old.txt").read_text() == "old"
    [other] = [p for p in _stale_siblings() if p != taken]
    assert (other / "new.txt").read_text() == "new"


def test_registered_worktree_is_reused_and_nothing_moved(monkeypatch):
    _dest().mkdir(parents=True)
    git = FakeGit(listing=f"worktree {_dest()}\nHEAD 1111\nbranch refs/heads/pl/{_slug()}\n\n")
    monkeypatch.setattr(worktrees, "_run", git)
    assert REAL_FOR_RUN(CARD) == (_dest(), None)
    assert git.adds() == [] and _stale_siblings() == []


def test_folder_that_cannot_be_moved_is_an_error(monkeypatch):
    git = FakeGit()
    monkeypatch.setattr(worktrees, "_run", git)
    _dest().mkdir(parents=True)
    (_dest() / "leftover.txt").write_text("keep")

    def deny(*a):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(worktrees.os, "rename", deny)
    path, err = REAL_FOR_RUN(CARD)
    assert path is None and str(_dest()) in err and "could not set aside" in err
    assert (_dest() / "leftover.txt").exists()
    assert git.adds() == []


def test_set_aside_records_one_event(monkeypatch, tmp_path):
    from pl import events
    C.STATE_DIR = tmp_path / "state"
    monkeypatch.setattr(worktrees, "_run", FakeGit())
    _dest().mkdir(parents=True)
    (_dest() / "leftover.txt").write_text("keep")
    REAL_FOR_RUN(CARD)
    [moved] = _stale_siblings()
    [ev] = [e for e in events._read() if e["kind"] == "worktree_set_aside"]
    assert ev["card"] == CARD["id"] and ev["path"] == str(_dest()) and ev["moved_to"] == str(moved)


def test_empty_folder_is_removed_not_set_aside(monkeypatch):
    monkeypatch.setattr(worktrees, "_run", FakeGit())
    _dest().mkdir(parents=True)
    assert REAL_FOR_RUN(CARD) == (_dest(), None)
    assert _stale_siblings() == []


def _order(git):
    return [tuple(a[1:3]) for a in git.calls]


def test_missing_folder_with_a_registration_is_pruned_and_added(monkeypatch):
    git = FakeGit()
    monkeypatch.setattr(worktrees, "_run", git)
    assert REAL_FOR_RUN(CARD) == (_dest(), None)
    order = _order(git)
    assert order.index(("worktree", "prune")) < order.index(("worktree", "list")) < order.index(("worktree", "add"))


def _real_repo(tmp_path):
    """A local repo with a bare origin, set as the work dir."""
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "HOME": str(tmp_path / "home"), "PATH": __import__("os").environ["PATH"]}

    def git(*args, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd or C.WORK_DIR, env=env, capture_output=True, text=True, check=True).stdout

    origin = tmp_path / "origin.git"
    git("init", "-q", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    git("init", "-q", "-b", "main", ".")
    git("commit", "-q", "--allow-empty", "-m", "first")
    git("remote", "add", "origin", str(origin))
    git("push", "-q", "origin", "main")
    git("remote", "set-head", "origin", "main")
    return git


def _registered(git):
    return [l.split(" ", 1)[1] for l in git("worktree", "list", "--porcelain").splitlines() if l.startswith("worktree ")]


def test_real_git_missing_folder_rebuilds(tmp_path, monkeypatch):
    git = _real_repo(tmp_path)
    monkeypatch.setattr(worktrees, "_run", subprocess.run)
    path, err = REAL_FOR_RUN(CARD)
    assert err is None
    import shutil
    shutil.rmtree(path)
    assert REAL_FOR_RUN(CARD) == (path, None)
    assert str(path) in _registered(git)


def test_real_git_folder_in_the_way_is_renamed_and_unregistered(tmp_path, monkeypatch):
    git = _real_repo(tmp_path)
    monkeypatch.setattr(worktrees, "_run", subprocess.run)
    path, err = REAL_FOR_RUN(CARD)
    assert err is None
    git("worktree", "remove", "--force", str(path))
    git("branch", "-D", f"pl/{_slug()}")
    path.mkdir()
    (path / "mine.txt").write_text("keep")
    assert REAL_FOR_RUN(CARD) == (path, None)
    [moved] = _stale_siblings()
    assert (moved / "mine.txt").read_text() == "keep"
    assert str(moved) not in _registered(git)


def test_real_git_stale_registered_folder_is_renamed_and_branch_freed(tmp_path, monkeypatch):
    git = _real_repo(tmp_path)
    monkeypatch.setattr(worktrees, "_run", subprocess.run)
    path, _ = REAL_FOR_RUN(CARD)
    git("-C", str(path), "checkout", "-q", "--detach")   # no longer on the card's branch: not reusable
    assert REAL_FOR_RUN(CARD) == (path, None)
    [moved] = _stale_siblings()
    assert str(moved) not in _registered(git) and str(path) in _registered(git)


def test_real_git_other_cards_worktree_is_untouched(tmp_path, monkeypatch):
    git = _real_repo(tmp_path)
    monkeypatch.setattr(worktrees, "_run", subprocess.run)
    other = {"id": "acme/app#8", "title": "Other card", "metadata": {}}
    other_path, err = REAL_FOR_RUN(other)
    assert err is None
    import shutil
    shutil.rmtree(_dest(), ignore_errors=True)
    path, err = REAL_FOR_RUN(CARD)
    assert err is None
    assert other_path.is_dir() and str(other_path) in _registered(git)


def _slice_listing(name):
    return f"worktree {_dest()}\nHEAD abc\nbranch refs/heads/pl/{name}\n\n"


def test_worktree_on_own_slice_branch_is_reused(monkeypatch):
    _dest().mkdir(parents=True)
    git = FakeGit(listing=_slice_listing(f"{_slug()}-s2"))
    monkeypatch.setattr(worktrees, "_run", git)
    assert REAL_FOR_RUN(CARD) == (_dest(), None)
    assert git.adds() == [] and _stale_siblings() == []


def test_slice_branch_of_another_cards_folder_is_not_claimed(monkeypatch):
    other = _dest().with_name(_dest().name + "-s2")
    other.mkdir(parents=True)
    git = FakeGit(listing=f"worktree {other}\nHEAD abc\nbranch refs/heads/pl/{_slug()}-s2\n\n")
    monkeypatch.setattr(worktrees, "_run", git)
    assert REAL_FOR_RUN(CARD) == (_dest(), None)
    assert len(git.adds()) == 1


def _failed_card(**meta):
    return {"id": "acme/app#7", "title": "Add thing", "list_id": "x", "metadata": meta}


def test_worktree_failure_marks_the_card_and_names_card_path_and_git_message(launch):
    launch.result = (None, "fatal: x")
    _start()
    [(args, kw)] = launch.updates
    text = kw["metadata"]["worktree_failed"]
    assert str(_dest()) in text and "fatal: x" in text
    alert = next(e for e in launch.events if e[0] == "alert")
    assert alert[1] == "worktree:acme/app#7"


def test_repeat_failure_writes_and_emits_once(launch):
    launch.result = (None, "fatal: x")
    card = {"id": "acme/app#7", "title": "Add thing", "metadata": {"worktree_failed": f"{_dest()}: fatal: x"}}
    dispatch.start_worker(card, "run", 0, False)
    assert launch.updates == []
    assert not [e for e in launch.events if e[0] == "error"]


def test_worktree_failed_card_reads_as_needs_a_person(monkeypatch):
    from pl import agents
    monkeypatch.setattr(agents, "col_name", lambda lid: "Approved")
    c = _failed_card(worktree_failed="p: fatal", pipeline_mode="auto")
    for text in (agents.worker_view(c, {}), agents.person_wait(c, "dead")):
        assert text == "worktree failed: needs a person" and "waiting" not in text


def test_next_good_start_resolves_alert_and_clears_state(launch):
    resolved = []
    launch.result = (launch.result[0], None)
    dispatch.alerts.resolve = lambda key: resolved.append(key)
    card = {"id": "acme/app#7", "title": "Add thing", "metadata": {"worktree_failed": "p: fatal"}}
    dispatch.start_worker(card, "run", 0, False)
    assert resolved == ["worktree:acme/app#7"]
    assert any(kw.get("metadata") == {"worktree_failed": None} for _, kw in launch.updates)


# --- Slice 4: the state of a folder is recorded before it is set aside ---


class InFolderGit(FakeGit):
    def __init__(self, dirty="", ahead="0", fail_in_folder=False, **kw):
        super().__init__(**kw)
        self.dirty, self.ahead, self.fail_in_folder = dirty, ahead, fail_in_folder

    def __call__(self, argv, **kw):
        sub = argv[1:]
        if kw.get("cwd") == str(_dest()):
            self.calls.append(argv)
            if self.fail_in_folder:
                return subprocess.CompletedProcess(argv, 128, "", "fatal")
            out = {"branch": "pl/feature\n", "rev-parse": "abc123\n", "status": self.dirty, "rev-list": f"{self.ahead}\n"}
            return subprocess.CompletedProcess(argv, 0, out[sub[0]], "")
        return super().__call__(argv, **kw)


def _registered_folder(monkeypatch, **kw):
    git = InFolderGit(**kw)
    monkeypatch.setattr(worktrees, "_run", git)
    _dest().mkdir(parents=True)
    (_dest() / ".git").write_text("gitdir: x")
    (_dest() / "work.txt").write_text("keep")
    return git


def _record_of(folder):
    return worktrees._read_stale_record(folder)


def test_set_aside_records_state_readable_after_rename(monkeypatch):
    _registered_folder(monkeypatch)
    REAL_FOR_RUN(CARD)
    [moved] = _stale_siblings()
    rec = _record_of(moved)
    assert (rec["card"], rec["branch"], rec["head"], rec["uncommitted"], rec["unpushed"]) == (CARD["id"], "pl/feature", "abc123", False, 0)
    assert rec["at"]


def test_set_aside_records_dirty_and_unpushed(monkeypatch):
    _registered_folder(monkeypatch, dirty=" M f\n", ahead="2")
    REAL_FOR_RUN(CARD)
    rec = _record_of(_stale_siblings()[0])
    assert (rec["uncommitted"], rec["unpushed"]) == (True, 2)


def test_set_aside_git_failure_writes_no_record(monkeypatch):
    _registered_folder(monkeypatch, fail_in_folder=True)
    REAL_FOR_RUN(CARD)
    assert _record_of(_stale_siblings()[0]) is None


def test_plain_folder_gets_no_record(monkeypatch):
    monkeypatch.setattr(worktrees, "_run", FakeGit())
    _dest().mkdir(parents=True)
    (_dest() / "x").write_text("x")
    REAL_FOR_RUN(CARD)
    assert not list(_stale_siblings()[0].glob(".pl-set-aside.json"))


def test_failed_rename_removes_record(monkeypatch):
    _registered_folder(monkeypatch)
    monkeypatch.setattr(worktrees.os, "rename", lambda *a: (_ for _ in ()).throw(OSError(13, "denied")))
    path, err = REAL_FOR_RUN(CARD)
    assert path is None and "denied" in err
    assert not (_dest() / ".pl-set-aside.json").exists()


def test_record_is_mode_0600(monkeypatch):
    _registered_folder(monkeypatch)
    REAL_FOR_RUN(CARD)
    assert (_stale_siblings()[0] / ".pl-set-aside.json").stat().st_mode & 0o777 == 0o600
