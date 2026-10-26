"""Plan 33 slice 4: sub-tab keys, the remembered sub-tab per profile, and the footer's view keys first."""
import json

import pytest
from textual.widgets import Footer, Input
from textual.widgets._footer import FooterKey

from pl import config as C
from pl.tui.app import PlApp
from pl.tui.chrome import MAIN_KEYS, groups
from test_tui_views import FakeAssistant, Provider, fake_home, settle  # noqa: F401 - fake_home is an autouse fixture there


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    d = tmp_path / "state"
    d.mkdir()
    monkeypatch.setattr(C, "STATE_DIR", d)
    return d


async def start(pilot):
    await settle(pilot)
    return pilot.app


async def test_ac1_right_and_left_switch_work_sub_tabs_from_the_list():
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(120, 40)) as pilot:
        await start(pilot)
        await pilot.press("3")
        await settle(pilot)
        assert app.active_tab == "ideas"
        for key, want in (("right", "cards"), ("left", "ideas"), ("left", "cards"), ("right", "ideas")):
            await pilot.press(key)
            await settle(pilot)
            assert app.active_tab == want


async def test_ac2_a_free_letter_opens_its_sub_tab():
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(120, 40)) as pilot:
        await start(pilot)
        await pilot.press("3", "b")
        await settle(pilot)
        assert app.active_tab == "cards"
        await pilot.press("5", "l")
        await settle(pilot)
        assert app.active_tab == "loops"
        label = app.query_one("#work-views").get_tab("cards").label
        assert label.plain.startswith("Board") and label.spans[0].style.underline and label.spans[0].start == 0


async def test_ac3_sub_tab_letters_never_collide():
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(120, 40)) as pilot:
        await start(pilot)
        assert app.sub_keys == {"assistant": {"b": "brain"}, "work": {"b": "cards"}, "automate": {"l": "loops"}}
        for gid, letters in app.sub_keys.items():
            own = {k for k in letters if all(b.action.startswith("sub_letter") for b in app._bindings.key_to_bindings[k])}
            assert own == set(letters)
            assert not any(k in w._bindings.key_to_bindings for k in letters for w in app.query(f"#g-{gid} *"))
        await pilot.press("3", "i")
        await settle(pilot)
        assert app.active_tab == "ideas"
        await pilot.press("7", "a")
        await settle(pilot)
        assert app.active_tab == "activity"


async def test_ac4_text_boxes_keep_left_right_and_letters(monkeypatch):
    FakeAssistant(monkeypatch)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(120, 40)) as pilot:
        await start(pilot)
        await pilot.press("2")
        await settle(pilot)
        box = app.focused
        assert isinstance(box, Input)
        await pilot.press("x", "b", "left")
        await settle(pilot)
        assert box.value == "xb" and box.cursor_position == 1
        assert app.active_tab == "assistant"


@pytest.mark.parametrize("key", ["1", "4", "7"])
async def test_ac5_single_view_tabs_ignore_left_right(key):
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(120, 40)) as pilot:
        await start(pilot)
        await pilot.press(key)
        await settle(pilot)
        before = app.active_tab
        await pilot.press("left", "right")
        await settle(pilot)
        assert app.active_tab == before


async def test_ac6_last_sub_tabs_come_back_after_a_restart(state_dir):
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(120, 40)) as pilot:
        await start(pilot)
        await pilot.press("3", "b", "5", "right")
        await settle(pilot)
    saved = json.loads((state_dir / "subtabs.json").read_text())
    assert saved["work"] == "cards" and saved["automate"] == "subagents"
    again = PlApp(snapshot_provider=Provider())
    async with again.run_test(size=(120, 40)) as pilot:
        await start(pilot)
        await pilot.press("3")
        await settle(pilot)
        assert again.active_tab == "cards"
        await pilot.press("5")
        await settle(pilot)
        assert again.active_tab == "subagents"


async def test_ac7_another_profile_starts_on_first_sub_tabs(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    (a / "subtabs.json").write_text('{"work": "cards"}')
    monkeypatch.setattr(C, "STATE_DIR", b)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(120, 40)) as pilot:
        await start(pilot)
        await pilot.press("3")
        await settle(pilot)
        assert app.active_tab == "ideas"
        await pilot.press("5")
        await settle(pilot)
        assert app.active_tab == "loops"
    assert (a / "subtabs.json").read_text() == '{"work": "cards"}'


@pytest.mark.parametrize("saved,assistant_off", [
    (None, False), ("not json", False), ("[1, 2]", False), ('{"work": "gone"}', False),
    ('{"assistant": "assistant"}', True)])
async def test_ac8_bad_saved_file_falls_back_quietly(state_dir, saved, assistant_off):
    if saved is not None:
        (state_dir / "subtabs.json").write_text(saved)
    if assistant_off:
        C.ASSISTANT = {"enabled": False}
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(120, 40)) as pilot:
        await start(pilot)
        first = {gid: views[0][0] for gid, _, views in groups()}
        for gid, vid in first.items():
            assert app.query_one(f"#{gid}-views").active == vid
        assert not app._notifications
        await pilot.press("3", "b")
        await settle(pilot)
    assert json.loads((state_dir / "subtabs.json").read_text())["work"] == "cards"


async def test_ac9_tab_option_wins_and_is_saved(state_dir):
    (state_dir / "subtabs.json").write_text('{"work": "ideas"}')
    app = PlApp(snapshot_provider=Provider(), tab="cards")
    async with app.run_test(size=(120, 40)) as pilot:
        await start(pilot)
        assert app.active_tab == "cards"
    assert json.loads((state_dir / "subtabs.json").read_text())["work"] == "cards"


def footer_keys(app):
    return [k for k in app.query_one(Footer).query(FooterKey)]


@pytest.mark.parametrize("view", sorted(MAIN_KEYS))
async def test_ac10_footer_at_80x24_shows_each_views_main_keys(view):
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(80, 24)) as pilot:
        await start(pilot)
        if view not in {v for _, _, vs in groups() for v, _ in vs}:
            pytest.skip("view not shown")
        app.action_tab(view)
        await settle(pilot)
        keys = footer_keys(app)
        for k in keys:
            if k.action in MAIN_KEYS[view] or k.action == "help":
                assert k.region.width > 0 and k.region.right <= 80, (view, k.action, k.region)
        if not app._typing():   # a printable key in a text box is text, so the footer drops those bindings
            assert any(k.action == "help" for k in keys), [k.action for k in keys]


async def test_footer_keeps_console_keys_after_view_keys():
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(200, 24)) as pilot:
        await start(pilot)
        app.action_tab("cards")
        await settle(pilot)
        actions = [k.action for k in footer_keys(app)]
        assert actions.index("move_column") < actions.index("dispatcher")
