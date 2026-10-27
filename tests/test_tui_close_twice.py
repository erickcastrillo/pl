"""A screen's close action is safe to run twice (a quick double Esc): the second one does nothing.

2026-10-26: Esc on a card screen raised ScreenStackError ("Can't pop screen; there must be at least one screen on
the stack") and took the console down, because the close action popped whatever screen was on top."""
from textual.app import App
from textual.screen import Screen

from pl.tui.chrome import close


class _Card(Screen):
    def action_close(self):
        close(self)


async def test_closing_a_screen_twice_closes_it_once_and_keeps_the_console():
    app = App()
    async with app.run_test() as pilot:
        card = _Card()
        app.push_screen(card)
        await pilot.pause()
        card.action_close()
        card.action_close()   # already closed: nothing to do, no error
        await pilot.pause()
        assert len(app.screen_stack) == 1


async def test_close_leaves_a_screen_opened_on_top_of_it_alone():
    app = App()
    async with app.run_test() as pilot:
        card, other = _Card(), Screen()
        app.push_screen(card)
        app.push_screen(other)
        await pilot.pause()
        card.action_close()   # not the top screen: closing it would pop the wrong one
        await pilot.pause()
        assert app.screen is other
