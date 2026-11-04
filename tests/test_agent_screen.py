"""Reading an agent's tmux pane: capture (masked, colors kept) and the status word. A fake runner stands in for tmux."""
import subprocess

import pytest

from pl import agent_screen, harnesses

TOKEN = "ghp_" + "a" * 36


class FakeTmux:
    """Answers tmux subcommands from a dict: capture-pane -> screen text, display-message -> pane command."""

    def __init__(self, screen="", cmd="claude", code=0, raises=None):
        self.screen, self.cmd, self.code, self.raises, self.calls = screen, cmd, code, raises, []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        if self.raises:
            raise self.raises
        out = self.screen if argv[1] == "capture-pane" else self.cmd + "\n"
        return subprocess.CompletedProcess(argv, self.code, out, "")


@pytest.fixture
def tmux(monkeypatch):
    def install(**kw):
        fake = FakeTmux(**kw)
        monkeypatch.setattr(harnesses, "_run", fake)
        return fake
    return install


def test_working_agent_says_working(tmux):
    tmux(screen="● Reading src/pl/board.py\n● Running tests\n\n\n")
    text, word, why = agent_screen.read("%1")
    assert (word, why) == ("working", "")
    assert text.endswith("Running tests")


def test_waiting_agent_names_what_it_waits_on(tmux):
    tmux(screen="doing things\nGATE: not-approved\n")
    assert agent_screen.read("%1")[1:] == ("waiting for you", "GATE: not-approved")
    tmux(screen="Do you want to proceed?\n❯ 1. Yes\n  2. No\n")
    word, why = agent_screen.read("%1")[1:]
    assert (word, why) == ("waiting for you", "Do you want to proceed?")


def test_usage_limit_screen_says_stopped_usage_limit(tmux):
    tmux(screen="work\nClaude usage limit reached\n")
    assert agent_screen.read("%1")[1:] == ("stopped", "usage limit")


def test_missing_prerequisite_says_stopped_and_the_line(tmux):
    tmux(screen="running\nzsh: gh: command not found\n")
    word, why = agent_screen.read("%1")[1:]
    assert word == "stopped" and why == "missing prerequisite: zsh: gh: command not found"


def test_error_line_says_stopped_error(tmux):
    tmux(screen="API Error: 500 overloaded\n")
    assert agent_screen.read("%1")[1:] == ("stopped", "error: API Error: 500 overloaded")


def test_exited_agent_says_finished_with_its_last_message(tmux):
    tmux(screen="All done, PR opened.\n$ \n\n", cmd="zsh")
    word, why = agent_screen.read("%1")[1:]
    assert word == "finished" and why == "$"
    tmux(screen="All done, PR opened.\n\n", cmd="zsh")
    assert agent_screen.read("%1")[1:] == ("finished", "All done, PR opened.")


def test_why_is_cut_to_120_characters(tmux):
    tmux(screen="GATE: not-approved " + "x" * 300 + "\n")
    assert len(agent_screen.read("%1")[2]) == 120


def test_missing_tmux_or_window_says_no_agent_window(tmux):
    for kw in ({"raises": FileNotFoundError("tmux")}, {"raises": OSError("boom")}, {"code": 1}):
        tmux(**kw)
        assert agent_screen.read("%1") == (None, "no agent window", "")
    fake = tmux()
    assert agent_screen.read("") == (None, "no agent window", "")
    assert fake.calls == []


def test_capture_masks_secrets_and_keeps_ansi(tmux):
    tmux(screen=f"\x1b[31mred\x1b[0m token {TOKEN}\n")
    text = agent_screen.capture("%1")
    assert TOKEN not in text and "***" in text and "\x1b[31m" in text
    assert agent_screen.plain(text) == "red token ***"


def test_capture_reads_only_the_given_pane_and_line_count(tmux):
    fake = tmux(screen="x\n")
    agent_screen.capture("%7")
    assert fake.calls == [["tmux", "capture-pane", "-p", "-e", "-J", "-t", "%7", "-S", "-20"]]
    fake.calls.clear()
    agent_screen.capture("%7", lines=2000)
    assert fake.calls[0][-2:] == ["-S", "-2000"]
