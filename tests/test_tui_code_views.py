"""Plan 44 slice 4: the engineering-only views follow the pipeline. Fake profiles, no real HOME, tmux or network."""
import pytest
from textual.app import App
from textual.widgets import TabbedContent

from pl import config as C
from pl import pipeline
from pl.tui import chrome, loops, settings
from pl.tui.app import PlApp
from pl.tui.switcher import SwitcherScreen
from test_tui_views import Provider, fake_home, settle  # noqa: F401  (fake_home is an autouse fixture)

NOTICE = "not used in this pipeline"


def _owner(name):
    return {"column": name, "done": f"{name} done", "skill": "pl-stage", "gate": "owner"}


SALES = {"name": "sales", "template": None, "columns": ["Inbox"], "stages": {"lead": _owner("Inbox"), "offer": _owner("Lead")}}
SUPPORT = {"name": "support", "template": None, "columns": ["Inbox"],
           "stages": {"reply": {**_owner("Inbox"), "outward": True}}}


def _eng():
    return pipeline.engineering()


@pytest.fixture
def sales():
    C.PIPELINE = SALES


@pytest.fixture
def support():
    C.PIPELINE = SUPPORT


@pytest.fixture
def eng():
    C.PIPELINE = _eng()


def _notes(app):
    return " | ".join(str(n.message) for n in app._notifications)


def test_has_code_detects_auto_merge_or_pl_run():
    C.PIPELINE = _eng()
    assert pipeline.has_code()
    C.PIPELINE = None
    assert pipeline.has_code()
    C.PIPELINE = {"name": "x", "stages": {}}
    assert pipeline.has_code()
    assert not any(map(lambda p: (setattr(C, "PIPELINE", p), pipeline.has_code())[1], (SALES, SUPPORT)))
    C.PIPELINE = {**SALES, "stages": {**SALES["stages"], "build": {"column": "A", "done": "B", "skill": "pl-run"}}}
    assert pipeline.has_code()
    C.PIPELINE = {**SALES, "stages": {"build": {"column": "A", "done": "B", "skill": "pl-stage", "gate": "auto-merge"}}}
    assert pipeline.has_code()


async def test_engineering_console_shows_pull_requests_as_today(eng):
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test() as pilot:
        await settle(pilot)
        assert app.query("#prs")
        assert ("prs", "Pull requests") in chrome.tabs()


async def test_sales_console_has_no_pull_requests_and_keeps_review_key(sales):
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test() as pilot:
        await settle(pilot)
        assert not app.query("#prs")
        assert str(app.query_one("#tabs", TabbedContent).get_tab("g-review").label) == "4 Review"
        assert chrome.GROUP_KEYS["automate"] == "5"
        app.action_refresh()
        await settle(pilot)
        assert app.error is None


async def test_sales_prs_says_not_used_and_key_4_still_opens_review(sales):
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test() as pilot:
        await settle(pilot)
        app.action_tab("prs")
        await settle(pilot)
        assert app.active_tab == "dashboard" and NOTICE in _notes(app)
        SwitcherScreen._select_item(type("S", (), {"dismiss": lambda s, v: None, "app": app})(), {"kind": "tab", "tab_id": "prs"})
        assert app.active_tab == "dashboard"
        await pilot.press("4")
        await settle(pilot)
        assert app.active_tab == "waiting"


async def test_support_template_hides_pull_requests_too(support):
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test() as pilot:
        await settle(pilot)
        assert not app.query("#prs")
        app.action_tab("prs")
        await settle(pilot)
        assert NOTICE in _notes(app)


def _loop_setup():
    C.SERVICES = {"merge-gate": {"prompt": "/loop 5m x"}, "triage": {"prompt": "/loop 5m y"}}
    C.LOOPS_OFF = {}
    C.TRACKER = {"type": "github-project"}


def _names():
    return [r["name"] for r in loops.loop_rows({"snapshot": {"rows": []}})]


def test_sales_loops_have_no_merge_rows(sales):
    _loop_setup()
    names = _names()
    assert "triage" in names and "merge-gate" not in names and "auto-merge" not in names


def test_engineering_loops_keep_merge_rows(eng):
    _loop_setup()
    assert {"merge-gate", "auto-merge", "triage"} <= set(_names())


def test_paused_merge_gate_is_hidden_in_sales(sales):
    _loop_setup()
    C.SERVICES, C.LOOPS_OFF = {"triage": C.SERVICES["triage"]}, {"merge-gate": {"prompt": "/loop 5m x"}}
    assert "merge-gate" not in _names()


class _Host(App):
    def compose(self):
        yield settings.SettingsView()


@pytest.mark.parametrize("fixture, shown", [("eng", True), ("sales", False)])
async def test_settings_worktrees_field_follows_the_pipeline(request, fixture, shown):
    request.getfixturevalue(fixture)
    async with _Host().run_test() as pilot:
        await settle(pilot)
        paths = [f[0] for f in pilot.app.query_one(settings.SettingsView).fields.values()]
        assert (("paths", "worktrees_dir") in paths) is shown
