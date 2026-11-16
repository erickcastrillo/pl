"""The dispatcher types a prompt into an idle session once, into an empty box, and checks that it was submitted."""
import pytest

from pl import alerts, dispatch


class FakePane:
    """A prompt box as a list of lines; Enter submits unless swallow_enters is left."""

    def __init__(self, lines=(), swallow_enters=0, ignore_clear=False):
        self.lines, self.submitted, self.typed = list(lines) or [""], [], []
        self.swallow, self.ignore_clear = swallow_enters, ignore_clear

    def tmux(self, *a, **k):
        if a[0] == "capture-pane":
            body = [("❯ " if i == 0 else "  ") + ln for i, ln in enumerate(self.lines)]
            return "\n".join(["────", *body, "────", "footer"])
        if a[0] != "send-keys":
            return ""
        keys = a[a.index("-t") + 2:]
        if keys[0] == "-l":
            self.typed.append(keys[-1])
            self.lines[-1] += keys[-1]
        elif keys[0] == "C-u" and not self.ignore_clear:
            self.lines[-1] = ""
        elif keys[0] == "BSpace" and not self.ignore_clear and len(self.lines) > 1 and self.lines[-1] == "":
            self.lines.pop()
        elif keys[0] == "Enter":
            if self.swallow:
                self.swallow -= 1
            else:
                text = " ".join(x for x in self.lines if x)
                if text:
                    self.submitted.append(text)
                self.lines = [""]
        return ""


@pytest.fixture
def pane(monkeypatch):
    def make(**kw):
        p = FakePane(**kw)
        monkeypatch.setattr(dispatch, "tmux", p.tmux)
        return p
    return make


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(dispatch, "TYPE_SETTLE", 0)
    dispatch._SENT.clear()


def test_a_multi_line_leftover_is_cleared_before_typing(pane):
    prompt = "run the review"
    p = pane(lines=[prompt, prompt, "more"])
    assert dispatch._type_into("%1", prompt) is True
    assert p.submitted == [prompt]


def test_a_long_or_slash_prompt_is_submitted(pane):
    for text in ("x" * 600, "/pl-review repo=x"):
        p = pane(swallow_enters=1)
        assert dispatch._type_into("%1", text) is True
        assert p.submitted == [text] and p.lines == [""]


def test_a_box_that_will_not_clear_types_nothing(pane):
    p = pane(lines=["old"], ignore_clear=True)
    assert dispatch._type_into("%1", "new") is False
    assert p.typed == []


@pytest.fixture
def clock(monkeypatch):
    t = {"now": 1000.0}
    monkeypatch.setattr(dispatch.time, "time", lambda: t["now"])
    return t


@pytest.fixture
def alert_log(monkeypatch):
    log = []
    monkeypatch.setattr(alerts, "open", lambda key, *a, **k: log.append(("open", key)))
    monkeypatch.setattr(alerts, "resolve", lambda key: log.append(("resolve", key)))
    return log


def test_busy_within_the_window_needs_no_alert(pane, clock, alert_log):
    p = pane()
    assert dispatch._typed_turn("prompt_unsent:x", "%1", "go", busy=False) == "typed"
    clock["now"] += 10
    assert dispatch._typed_turn("prompt_unsent:x", "%1", "go", busy=True) == "done"
    assert p.submitted == ["go"] and not [a for a in alert_log if a[0] == "open"]
    assert "prompt_unsent:x" not in dispatch._SENT


def test_still_idle_after_the_window_retries_once_then_alerts(pane, clock, alert_log):
    p = pane(swallow_enters=99)
    results = []
    for _ in range(4):
        results.append(dispatch._typed_turn("prompt_unsent:x", "%1", "go", busy=False))
        clock["now"] += 31
    assert results == ["typed", "typed", "alerted", "alerted"]
    assert p.typed == ["go", "go"]
    assert alert_log.count(("open", "prompt_unsent:x")) >= 1


def test_the_alert_resolves_when_the_session_goes_busy(pane, clock, alert_log):
    pane(swallow_enters=99)
    for _ in range(3):
        dispatch._typed_turn("prompt_unsent:x", "%1", "go", busy=False)
        clock["now"] += 31
    assert dispatch._typed_turn("prompt_unsent:x", "%1", "go", busy=True) == "done"
    assert ("resolve", "prompt_unsent:x") in alert_log


def test_foreign_text_is_left_and_alerted(pane, clock, alert_log):
    p = pane(lines=["something the owner wrote"])
    assert dispatch._typed_turn("prompt_unsent:x", "%1", "go", busy=False) == "alerted"
    assert not [a for a in p.typed] and p.lines == ["something the owner wrote"]
    assert ("open", "prompt_unsent:x") in alert_log
