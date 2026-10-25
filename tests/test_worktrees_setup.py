import argparse
import os
import time
import types

import pytest

from pl import commands, dispatch, harnesses, worktrees
from pl import config as C


def _manifest(repo, text):
    (repo / ".pl").mkdir(exist_ok=True)
    (repo / ".pl" / "worktree.yaml").write_text(text)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "main"
    r.mkdir()
    return r


@pytest.fixture
def wt(tmp_path):
    w = tmp_path / "wt"
    w.mkdir()
    return w


def _bootstrap(repo, wt, **kw):
    failures = []
    notes = worktrees.bootstrap_worktree(repo, wt, failures=failures, **kw)
    return notes, failures


def test_setup_executes_in_worktree_cwd(repo, wt):
    _manifest(repo, 'setup: "pwd > where.txt"\n')
    _, failures = _bootstrap(repo, wt)
    assert failures == []
    assert (wt / "where.txt").read_text().strip() == str(wt.resolve())


def test_setup_dry_run_prints_command_and_timeout(repo, wt):
    _manifest(repo, 'setup: "touch ran"\ntimeout: 42\n')
    notes, _ = _bootstrap(repo, wt, dry_run=True)
    assert 'setup: "touch ran" (timeout: 42s)' in notes
    assert not (wt / "ran").exists()


def test_setup_default_timeout_is_300(repo, wt):
    _manifest(repo, 'setup: "touch ran"\n')
    notes, _ = _bootstrap(repo, wt, dry_run=True)
    assert 'setup: "touch ran" (timeout: 300s)' in notes
    assert worktrees.parse_timeout(None) == 300


@pytest.mark.parametrize("bad", ["-5", "abc", "0", "1.5"])
def test_setup_invalid_or_negative_timeout_aborts(repo, wt, bad):
    _manifest(repo, f'setup: "touch ran"\ntimeout: {bad}\n')
    with pytest.raises(ValueError, match=f"invalid timeout '{bad}'"):
        _bootstrap(repo, wt)
    assert not (wt / "ran").exists()


def test_no_setup_or_empty_string_runs_no_subprocess(repo, wt, monkeypatch):
    monkeypatch.setattr(worktrees, "_popen", lambda *a, **k: pytest.fail("a subprocess started"))
    for text in ("copy: []\n", 'setup: ""\n'):
        _manifest(repo, text)
        assert _bootstrap(repo, wt) == ([], [])


def test_setup_timeout_terminates_process_group(repo, wt):
    _manifest(repo, 'setup: "sleep 60 & echo $! > child.pid; wait"\ntimeout: 1\n')
    _, failures = _bootstrap(repo, wt)
    assert failures[0]["reason"] == "Bootstrap setup timed out after 1s"
    pid = int((wt / "child.pid").read_text())
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        os.kill(pid, 9)
        pytest.fail("the child of the setup command survived the timeout")


def test_setup_logs_to_runs_dir_when_card_provided(repo, wt, tmp_path):
    _manifest(repo, 'setup: "echo out; echo err >&2"\n')
    log = tmp_path / "state" / "runs" / "c1-setup.log"
    _bootstrap(repo, wt, log_path=log)
    assert sorted(log.read_text().split()) == ["err", "out"]


def test_setup_logs_to_worktree_root_when_card_omitted(repo, wt):
    _manifest(repo, 'setup: "echo hi"\n')
    _bootstrap(repo, wt)
    assert (wt / ".pl-setup.log").read_text() == "hi\n"


def test_setup_failure_reports_exit_code_and_last_20_lines(repo, wt):
    _manifest(repo, 'setup: "seq 1 30; exit 3"\n')
    _, failures = _bootstrap(repo, wt)
    assert failures[0]["reason"] == "Bootstrap setup failed with exit code 3"
    assert failures[0]["output"].splitlines() == [str(i) for i in range(11, 31)]


class FakeBoard:
    def __init__(self):
        self.card = {"id": "c" * 36, "description": "# PIPELINE: INPUT\nidea\n", "metadata": {}}
        self.updates = []

    def find_card(self, token):
        return self.card

    def update(self, item_id, **fields):
        self.updates.append((item_id, fields))


@pytest.fixture
def board(monkeypatch):
    b = FakeBoard()
    monkeypatch.setattr(commands, "find_card", b.find_card)
    monkeypatch.setattr(commands, "update", b.update)
    return b


def _cmd(repo, wt, monkeypatch, card, tmp_path, tracker_dir):
    monkeypatch.setattr(C, "STATE_DIR", tracker_dir, raising=False)
    monkeypatch.setattr(worktrees, "main_checkout", lambda cwd: repo)
    commands.cmd_worktrees_bootstrap(argparse.Namespace(path=str(wt), dry_run=False, card=card))


def test_setup_failure_appends_note_to_card_with_exit_code_and_tail(repo, wt, tmp_path, monkeypatch, board):
    _manifest(repo, 'setup: "echo boom; exit 2"\n')
    _cmd(repo, wt, monkeypatch, "c1", tmp_path, tmp_path / "state")
    assert (tmp_path / "state" / "runs" / "c1-setup.log").read_text() == "boom\n"
    item_id, fields = board.updates[-1]
    assert fields["metadata"]["bootstrap_error"] == {"reason": "Bootstrap setup failed with exit code 2", "output": "boom"}
    assert "Bootstrap setup failed with exit code 2:\nboom" in fields["description"]


def test_setup_timeout_appends_note_to_card_with_timeout_limit_and_tail(repo, wt, tmp_path, monkeypatch, board):
    _manifest(repo, 'setup: "echo slow; sleep 30"\ntimeout: 1\n')
    _cmd(repo, wt, monkeypatch, "c1", tmp_path, tmp_path / "state")
    fields = board.updates[-1][1]
    assert "Bootstrap setup timed out after 1s:\nslow" in fields["description"]
    assert fields["metadata"]["bootstrap_error"]["reason"] == "Bootstrap setup timed out after 1s"


def test_a_successful_setup_with_card_leaves_the_card_alone(repo, wt, tmp_path, monkeypatch, board):
    _manifest(repo, 'setup: "true"\n')
    _cmd(repo, wt, monkeypatch, "c1", tmp_path, tmp_path / "state")
    assert board.updates == []


def _prompt_for(monkeypatch, metadata):
    seen = []
    monkeypatch.setattr(C, "PROMPTS", {**C.PROMPTS, "run": C.BUILTIN_PROMPTS["run"]}, raising=False)
    monkeypatch.setattr(C, "CODE_HOST", {}, raising=False)
    monkeypatch.setattr(C, "WORK_DIR", "/w", raising=False)
    monkeypatch.setattr(C, "ATTENTION", None, raising=False)
    monkeypatch.setattr(C, "TMUX_SESSION", "pl-t", raising=False)
    monkeypatch.setattr(dispatch.harnesses, "harness_for", lambda stage, c: (harnesses.get("claude"), "a"))
    monkeypatch.setattr(dispatch.harnesses, "unattended", lambda h: h)
    monkeypatch.setattr(dispatch.harnesses, "refuse_bypass", lambda h: None)
    monkeypatch.setattr(dispatch.harnesses, "launch_script", lambda h, prof, p, sid, label, **kw: seen.append(p) or "")
    monkeypatch.setattr(dispatch.context, "launch_prompt", lambda c, stage, p: p)
    monkeypatch.setattr(dispatch, "memory_arg", lambda c, stage, dry: "")
    monkeypatch.setattr(dispatch.subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=0))
    monkeypatch.setattr(dispatch, "tmux", lambda *a, **k: "%1")
    monkeypatch.setattr(dispatch, "_launch", lambda *a, **k: None)
    monkeypatch.setattr(dispatch, "update", lambda *a, **k: None)
    monkeypatch.setattr(dispatch.events, "emit", lambda *a, **k: None)
    dispatch.start_worker({"id": "c" * 36, "title": "t", "metadata": metadata}, "run", 0, False)
    return seen[0]


def test_dispatch_injects_bootstrap_failure_warning_into_run_prompt(monkeypatch):
    err = {"reason": "Bootstrap setup failed with exit code 2", "output": "npm ERR! nope"}
    prompt = _prompt_for(monkeypatch, {"bootstrap_error": err})
    assert "## WARNING: Worktree bootstrap failed\nReason: Bootstrap setup failed with exit code 2\n" in prompt
    assert "Last 20 lines of output:\nnpm ERR! nope" in prompt


def test_dispatch_injects_bootstrap_timeout_warning_into_run_prompt(monkeypatch):
    err = {"reason": "Bootstrap setup timed out after 300s", "output": "still installing"}
    assert "Reason: Bootstrap setup timed out after 300s" in _prompt_for(monkeypatch, {"bootstrap_error": err})


def test_dispatch_leaves_prompt_clean_on_successful_card(monkeypatch):
    assert "WARNING" not in _prompt_for(monkeypatch, {})
