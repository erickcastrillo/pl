"""AC-2: every console tab opens and every key its footer shows does what it says; 16d; the items an agent's behaviour decides."""
import hashlib

import pytest
from textual.widgets import Footer
from textual.widgets._footer import FooterKey

from conftest import press
from pl import config as C
from tui_wait import settle, wait_until

TAB_KEYS = (("dashboard", "1"), ("assistant", "2"), ("work", "3"), ("review", "4"), ("automate", "5"),
            ("extend", "6"), ("activity", "7"), ("settings", "8"), ("home", "H"))
NOT_PRESSED = {"q", "D", "K", "N", "U"}   # quit, start a dispatcher, flip the kill switch, restart, update: they act outside the throwaway console


def shown_keys(app):
    return [k.key for k in app.query_one(Footer).query(FooterKey) if k.key not in NOT_PRESSED]


def test_unmarked_run_skips_the_suite(request):
    marks = {m.name for m in request.node.iter_markers()}
    assert "e2e_ui" in marks
    assert ("skip" in marks) == ("e2e_ui" not in request.config.option.markexpr)


@pytest.mark.parametrize("tab,key", TAB_KEYS, ids=[t for t, _ in TAB_KEYS])
async def test_tab(console, tab, key):
    async with console() as pilot:
        app = pilot.app
        await press(pilot, key)
        await wait_until(pilot, lambda: app.active_group() == tab, f"the {tab} tab", timeout=20.0)
        for letter in list(app.sub_keys.get(tab, {})):
            await press(pilot, letter)
            await settle(pilot)
            for footer_key in shown_keys(app):
                depth = len(app.screen_stack)
                await press(pilot, footer_key)
                await settle(pilot)
                assert app._exception is None, f"{footer_key} crashed the console"
                while len(app.screen_stack) > depth:
                    await press(pilot, "escape")
                    await settle(pilot)
                await press(pilot, key, letter)
        assert app._exception is None


def snapshot(folder):
    return {str(p.relative_to(folder)): (p.stat().st_size, p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
            for p in sorted(folder.rglob("*")) if p.is_file()}


async def test_item_16d_real_profile_folder_unchanged(console, tmp_path, monkeypatch):
    real = tmp_path / "real" / ".pl-real"
    (real / "state").mkdir(parents=True)
    (real / "config.toml").write_text('[tracker]\ntype = "local"\n')
    (real / "state" / "dispatch.json").write_text("{}")
    before = snapshot(real)
    throwaway = C.CONFIG_DIR
    for var in ("PL_CONFIG_DIR", "PL_PROFILE", "PL_WORKTREES_DIR"):
        monkeypatch.setenv(var, str(real))
    async with console() as pilot:
        await press(pilot, "3")
        await settle(pilot)
        await press(pilot, "1")
        await settle(pilot)
        assert C.CONFIG_DIR == throwaway != real
    assert snapshot(real) == before


NOT_TESTABLE = {
    "7": "agents running tests in the foreground is agent behaviour the console cannot see",
    "15": "files deleted under /tmp by an unknown cause",
    "16": "needs a Vikunja server",
    "16c": "an agent using the main checkout is agent behaviour the console cannot see",
    "16e": "an agent starting load tests is agent behaviour the console cannot see",
    "16h": "agent behaviour the console cannot see",
    "16i": "agent behaviour the console cannot see",
    "16n": "agent behaviour the console cannot see",
}


def _not_testable(item, why):
    def test():
        pytest.skip(f"not testable here: {why}")
    test.__name__ = f"test_item_{item}_not_testable_here"
    return test


for _item, _why in NOT_TESTABLE.items():
    globals()[f"test_item_{_item}_not_testable_here"] = _not_testable(_item, _why)
