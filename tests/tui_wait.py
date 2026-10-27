"""Shared waits for console (Textual) tests: wait for a condition with a time limit, never a fixed pause."""
import re
import time

import pytest
from textual.app import App
from textual.css.query import NoMatches

PAUSE = re.compile(r"pilot\.pause\(")


async def wait_until(pilot, condition, what, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if condition():
                return
        except NoMatches:
            pass
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()   # no delay: only drains the message queue between checks
    pytest.fail(f"timed out after {timeout}s waiting for {what}")


async def wait_for_screen(pilot, name):
    await wait_until(pilot, lambda: name in {c.__name__ for c in type(pilot.app.screen).__mro__}, f"the {name}")


async def settle(pilot, condition=None, what="the app to settle"):
    await pilot.app.workers.wait_for_complete()
    if condition is None:
        await pilot.pause()   # one queue drain so messages posted by the workers are handled
    else:
        await wait_until(pilot, condition, what)


def view_app(make_widget, css=None):
    class ViewApp(App):
        CSS = css or ""

        def compose(self):
            yield make_widget()

    return ViewApp()


def uncommented_pauses(path):
    lines = path.read_text().splitlines()
    return [n for n, line in enumerate(lines, 1)
            if PAUSE.search(line) and "#" not in line.split("pilot.pause(", 1)[1]
            and not (n > 1 and lines[n - 2].lstrip().startswith("#"))]
