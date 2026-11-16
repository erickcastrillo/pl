"""question_wait: a multiple-choice question on an idle agent's screen, found from the pane."""
import time
import types

import pytest

from pl import agents, harnesses

AGY_QUESTION = """\
● Counted slice 3: 499 lines.
  git diff --numstat origin/main...HEAD

Slice 3 of 5 is 499 lines, over max_lines 300. How should this be handled?

  ● 1. Stop and report
    2. Open the slice anyway
    3. Split the slice

  enter to select · ↑/↓ to navigate
  esc to cancel
"""
CLAUDE_QUESTION = """\
● Reading the plan.

Which database should I use?

❯ 1. Postgres
  2. SQLite
  3. Type something.

Enter to select · ↑/↓ to navigate · Esc to cancel
"""
CODEX_QUESTION = """\
• Looking at the repo.

Which test runner should I use?

› 1. pytest
  2. unittest

enter to submit · ↑/↓ to move
"""
CLAUDE_PERMISSION = """\
Bash command
  rm -rf build

Do you want to proceed?
❯ 1. Yes
  2. Yes, and don't ask again for rm commands
  3. No

Esc to cancel
"""
AGY_WORKING = """\
● grep -rn "is it ready?" src
  src/a.py:3: # is it ready?
● Editing src/b.py

  esc to cancel
"""


@pytest.fixture
def pane(monkeypatch):
    s = {"text": "", "age": 300}

    def run(argv, **kw):
        if argv[1] == "capture-pane":
            return types.SimpleNamespace(returncode=0, stdout=s["text"])
        return types.SimpleNamespace(returncode=0, stdout=f"{int(time.time()) - s['age']}\n")
    monkeypatch.setattr(harnesses, "_run", run)
    return s


def _ask(name, pane_state, text):
    pane_state["text"] = text
    return agents.question_wait(harnesses.get(name), "%1")


def test_agy_question_returns_the_question_line_once_idle(pane):
    assert _ask("antigravity", pane, AGY_QUESTION) == "Slice 3 of 5 is 499 lines, over max_lines 300. How should this be handled?"


def test_question_not_reported_before_permission_idle(pane):
    pane["age"] = 30
    assert _ask("antigravity", pane, AGY_QUESTION) is None


@pytest.mark.parametrize("name,text,want", [("claude", CLAUDE_QUESTION, "Which database should I use?"),
                                            ("codex", CODEX_QUESTION, "Which test runner should I use?")])
def test_claude_and_codex_questions_are_found(pane, name, text, want):
    assert _ask(name, pane, text) == want


def test_a_permission_prompt_is_not_a_question(pane):
    assert _ask("claude", pane, CLAUDE_PERMISSION) is None


def test_a_question_mark_in_output_is_not_a_question(pane):
    assert _ask("antigravity", pane, AGY_WORKING) is None


def test_question_line_is_masked(pane):
    text = AGY_QUESTION.replace("Slice 3 of 5 is 499 lines, over max_lines 300. How should this be handled?",
                                "Use ghp_abcdefghijklmnopqrstuvwxyz0123456789 and $(rm -rf /) ok?")
    out = _ask("antigravity", pane, text)
    assert isinstance(out, str) and "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in out and out.endswith("ok?")
