"""Shared waits for console (Textual) tests: wait for a condition with a time limit, never a fixed pause."""
import re
import time

import pytest
from textual.app import App
from textual.css.query import NoMatches

PAUSE = re.compile(r"pilot\.pause\(")


async def wait_until(pilot, condition, what, timeout=15.0):
    """Returns as soon as condition() holds, so the limit costs a passing test nothing. 15 s, not 5: under a parallel
    run on a busy machine a console test runs about 3 times slower, and 5 s failed healthy tests.

    The first re-check only drains the message queue; later ones also give the app 10 ms. Textual lays the screen out
    when the app is idle, and a loop of zero-length pauses never lets it go idle, so a condition about layout (a
    region, a size) could stay false until the time limit."""
    deadline = time.monotonic() + timeout
    delay = 0
    while time.monotonic() < deadline:
        try:
            if condition():
                return
        except NoMatches:
            pass
        await pilot.app.workers.wait_for_complete()
        await pilot.pause(delay)
        delay = 0.01
    pytest.fail(f"timed out after {timeout}s waiting for {what}")


async def wait_for_screen(pilot, name):
    await wait_until(pilot, lambda: name in {c.__name__ for c in type(pilot.app.screen).__mro__}, f"the {name}")


async def settle(pilot, condition=None, what="the app to settle"):
    await pilot.app.workers.wait_for_complete()
    if condition is None:
        await pilot.pause(0)   # one queue drain so messages posted by the workers are handled
        await pilot.app.workers.wait_for_complete()
        await pilot.pause(0)   # drain callbacks posted from thread workers
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
