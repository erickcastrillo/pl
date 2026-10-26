"""What an agent's tmux window shows and what that means: its masked screen and a status word."""
import re

from rich.text import Text

from pl import agents, harnesses
from pl import config as C
from pl.util import mask

WHY_WIDTH = 120
PREREQ_RE = re.compile(r"command not found|No such file or directory|is not installed|not found in PATH|missing prerequisite", re.I)


def capture(pane, lines=20):
    """The last `lines` of a pane with its ANSI colors, secrets masked; None when there is no pane or tmux cannot read it."""
    if not pane:
        return None
    try:
        r = harnesses._run(["tmux", "capture-pane", "-p", "-e", "-J", "-t", pane, "-S", f"-{lines}"])
    except OSError:
        return None
    if r.returncode:
        return None
    return mask(r.stdout or "").rstrip()


def plain(text):
    """The text without ANSI codes, for reading it; never for display."""
    return Text.from_ansi(text).plain


def status(text, pane_cmd):
    """(word, why) for a captured screen and the pane's foreground command."""
    if text is None:
        return "no agent window", ""
    lines = [ln for ln in (agents.BOX_RE.sub(" ", ln).strip() for ln in plain(text).splitlines()) if ln][-8:]
    last = lines[-1] if lines else ""
    if pane_cmd in harnesses.SHELLS:
        return "finished", last[:WHY_WIDTH]
    if C.LIMIT_RE.search("\n".join(lines)):
        return "stopped", "usage limit"
    prereq = next((ln for ln in reversed(lines) if PREREQ_RE.search(ln)), None)
    if prereq:
        return "stopped", f"missing prerequisite: {prereq}"[:WHY_WIDTH]
    tail = lines[-3:]
    asked = next((ln for ln in reversed(tail) if agents.GATE_RE.search(ln) or ln.endswith("?")), None) \
        or next((ln for ln in reversed(tail) if agents.OPTION_RE.match(ln)), None)
    if asked:
        return "waiting for you", asked[:WHY_WIDTH]
    failed = next((ln for ln in reversed(lines) if agents.ERROR_LINE_RE.search(ln)), None)
    if failed:
        return "stopped", f"error: {failed}"[:WHY_WIDTH]
    return "working", ""


def read(pane, lines=20):
    """(text, word, why) of one pane, read once."""
    text = capture(pane, lines)
    return text, *status(text, harnesses.pane_command(pane) if text is not None else "")
