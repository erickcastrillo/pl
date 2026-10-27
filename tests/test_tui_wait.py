"""The shared console-test wait helper, and the guard that keeps fixed pauses out of the tests that use it."""
from pathlib import Path

import pytest
from textual.app import App
from textual.widgets import Static

from tui_wait import settle, uncommented_pauses, view_app, wait_for_screen, wait_until

TESTS = Path(__file__).parent


async def test_wait_until_returns_as_soon_as_the_condition_holds():
    flag = []

    class Host(App):
        def on_mount(self):
            self.run_worker(self.set_flag)

        async def set_flag(self):
            flag.append(1)

    async with Host().run_test() as pilot:
        await wait_until(pilot, lambda: flag, "the worker to set the flag", timeout=30)
        assert flag


async def test_wait_until_fails_with_what_it_waited_for():
    async with view_app(lambda: Static("hi")).run_test() as pilot:
        with pytest.raises(pytest.fail.Exception) as err:
            await wait_until(pilot, lambda: False, "a thing that never comes", timeout=0.2)
    assert "a thing that never comes" in str(err.value)
    assert "0.2s" in str(err.value)


async def test_wait_until_treats_a_missing_widget_as_not_yet():
    async with view_app(lambda: Static("hi", id="x")).run_test() as pilot:
        await wait_until(pilot, lambda: pilot.app.query_one("#x"), "the widget")
        with pytest.raises(pytest.fail.Exception):
            await wait_until(pilot, lambda: pilot.app.query_one("#nope"), "a missing widget", timeout=0.2)


async def test_settle_without_a_condition_returns():
    async with view_app(lambda: Static("hi")).run_test() as pilot:
        await settle(pilot)


async def test_view_app_mounts_only_the_given_widget():
    app = view_app(lambda: Static("hi", id="x"))
    async with app.run_test():
        assert app.query_one("#x")
        assert len(app.screen_stack) == 1
        assert type(app).__name__ != "PlApp"


async def test_wait_for_screen_sees_a_pushed_screen_by_class_name():
    from textual.screen import ModalScreen

    class Ask(ModalScreen):
        pass

    async with view_app(lambda: Static("hi")).run_test() as pilot:
        await pilot.app.push_screen(Ask())
        await wait_for_screen(pilot, "ModalScreen")
        await wait_for_screen(pilot, "Ask")


def test_uncommented_pauses_finds_bare_pauses_only(tmp_path):
    f = tmp_path / "t.py"
    f.write_text(
        "async def t(pilot):\n"
        "    await pilot.pause()\n"
        "    await pilot.pause()  # the tick is the thing under test\n"
        "    # nothing observable to wait for\n"
        "    await pilot.pause(0.1)\n"
        "    await wait_until(pilot, ok, 'x')\n"
    )
    assert uncommented_pauses(f) == [2]


def test_files_using_the_wait_helper_have_no_uncommented_pause():
    bad = [f"{p.name}:{n}" for p in sorted(TESTS.glob("test_*.py"))
           if p.name != Path(__file__).name and "tui_wait" in p.read_text()
           for n in uncommented_pauses(p)]
    assert not bad, f"fixed pilot.pause() without a comment saying why: {bad}"
