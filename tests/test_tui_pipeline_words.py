"""Plan 44 slice 2: the console says the pipeline's item words outside the Pipeline tab too."""
import pytest
from textual.widgets import DataTable

from pl import config as C
from pl import pipeline
from pl.tui import chrome, needs
from pl.tui import cards as tui_cards
from pl.tui import ideas as tui_ideas
from pl.tui import review as tui_review
from pl.tui.app import PlApp
from pl.tui.cards import CardScreen
from pl.tui.home import HomeView
from test_tui_views import Provider, _fake_card_read, _open_cards, fake_data, fake_home, screen_text, settle  # noqa: F401

pytestmark = pytest.mark.usefixtures("fake_home")

CID = "4a000001aaaa"


@pytest.fixture
def sales(monkeypatch):
    monkeypatch.setattr(C, "PIPELINE", {**pipeline.engineering(), "ui": {"item": "lead", "items": "leads", "fields": []}})


@pytest.fixture
def eng(monkeypatch):
    monkeypatch.setattr(C, "PIPELINE", pipeline.engineering())


def _notes(app):
    return [str(n.message) for n in app._notifications]


async def _card_screen(pilot, app):
    await settle(pilot)
    screen = CardScreen(CID, "Plan for review")
    await app.push_screen(screen)
    await settle(pilot)
    return screen


async def test_card_screen_says_lead_in_its_header_and_key_hints(sales, monkeypatch):
    _fake_card_read(monkeypatch, [])
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        screen = await _card_screen(pilot, app)
        assert screen.active_bindings["y"].binding.description == "copy lead text"
        assert "card" not in str(screen.query_one("#card-head").render()).lower()
        assert "card" not in " ".join(b.binding.description for b in screen.active_bindings.values())


async def test_card_screen_read_error_says_lead(sales, monkeypatch):
    def broken(cid):
        raise RuntimeError("boom")
    monkeypatch.setattr(tui_cards, "card", broken)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        screen = await _card_screen(pilot, app)
        assert "could not read the lead: boom" in str(screen.query_one("#card-head").render())


async def test_card_keys_toasts_say_lead(sales, monkeypatch):
    _fake_card_read(monkeypatch, [])
    app = PlApp(snapshot_provider=Provider(fake_data()))
    async with app.run_test(size=(176, 48)) as pilot:
        view = await _open_cards(pilot, app)
        monkeypatch.setattr(view, "_row", lambda: None)
        view.action_move_agent()
        view.action_move_column()
        await pilot.pause()
        assert "m moves a lead's live agent to another account" in _notes(app)
        assert "select a lead to move it to another column" in _notes(app)
        assert view._bindings.key_to_bindings["c"][0].description == "lead in browser"


async def test_engineering_card_screen_is_unchanged(eng, monkeypatch):
    _fake_card_read(monkeypatch, [])
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        screen = await _card_screen(pilot, app)
        assert screen.active_bindings["y"].binding.description == "copy card text"
        view = await _open_cards(pilot, app)
        assert view._bindings.key_to_bindings["c"][0].description == "card in browser"


async def test_home_cards_column_header_says_leads(sales):
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        table = app.query_one(HomeView).query_one(DataTable)
        assert [str(c.label) for c in table.columns.values()][:3] == ["pipeline", "status", "leads"]


def test_needs_you_detail_label_says_lead(sales):
    row = {"card": {"id": "abc", "title": "t", "metadata": {}, "tags": []}, "col": "Inbox", "worker": {}}
    lines = needs.detail(row).plain.splitlines()
    assert "lead     abc" in lines and not any(l.startswith("card") for l in lines)


@pytest.mark.parametrize("waiting,text", [(0, "no leads waiting"), (1, "1 lead waiting"), (2, "2 leads waiting")])
def test_status_sentence_says_leads_waiting(sales, waiting, text):
    snap = {"disp": "running", "summary": "", "rows": [], "needs": {"review": waiting}, "prof": ""}
    assert chrome.status_sentence(snap) == f"Running, {text}"


def test_engineering_needs_you_and_status_are_unchanged(eng):
    row = {"card": {"id": "abc", "title": "t", "metadata": {}, "tags": []}, "col": "Inbox", "worker": {}}
    assert "card     abc" in needs.detail(row).plain.splitlines()
    snap = {"disp": "running", "summary": "", "rows": [], "needs": {"review": 1}, "prof": ""}
    assert chrome.status_sentence(snap) == "Running, 1 card waiting"
    snap["needs"] = {}
    assert chrome.status_sentence(snap) == "Running, no cards waiting"


async def test_engineering_home_header_is_unchanged(eng):
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        table = app.query_one(HomeView).query_one(DataTable)
        assert [str(c.label) for c in table.columns.values()][:3] == ["pipeline", "status", "cards"]


def test_ideas_status_says_lead(sales):
    view = tui_ideas.IdeasView()
    assert view._status({"status": "approving"}) == "creating the lead…"


async def test_ideas_toasts_say_lead(sales):
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await pilot.press("3")
        view = app.query_one("IdeasView")
        view._approved({"id": "a" * 12, "status": "approving", "card_id": None})
        view._approved({"id": "a" * 12, "status": "approved", "card_id": "c" * 12})
        view.current = {"id": "a" * 12, "status": "approved"}
        view.action_discard()
        await pilot.pause()
        assert _notes(app) == ["another console is creating this lead",
                               "lead cccccccc created in Inbox; the spec stage picks it up",
                               "an approved idea is not discarded; its lead is on the board"]


async def test_engineering_ideas_are_unchanged(eng):
    assert tui_ideas.IdeasView()._status({"status": "approving"}) == "creating the card…"
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await pilot.press("3")
        view = app.query_one("IdeasView")
        view._approved({"id": "a" * 12, "status": "approving", "card_id": None})
        await pilot.pause()
        assert _notes(app) == ["another console is creating this card"]


def _moved_card(monkeypatch):
    monkeypatch.setattr(tui_review, "fresh_next", lambda: None)
    monkeypatch.setattr(tui_review, "card", lambda cid: {"id": cid, "list_id": "x", "description": ""})
    monkeypatch.setattr(tui_review, "col_name", lambda lid: "Approved")


def test_review_refusal_says_lead(sales, monkeypatch):
    _moved_card(monkeypatch)
    with pytest.raises(SystemExit, match=r"^the lead is now in 'Approved', not 'Spec ready': nothing written$"):
        tui_review.write_section("id", "SPEC", "", "new", column="Spec ready")


def test_review_slug_refusal_says_lead(sales, monkeypatch):
    monkeypatch.setattr(tui_review, "slug_of", lambda c: "Bad Slug!")
    with pytest.raises(SystemExit, match=r"^pl: lead abcdef12 has the slug"):
        tui_review.review_file("spec", {"id": "abcdef12" + "0" * 4})


def test_engineering_review_refusal_is_unchanged(eng, monkeypatch):
    _moved_card(monkeypatch)
    with pytest.raises(SystemExit, match=r"^the card is now in 'Approved', not 'Spec ready': nothing written$"):
        tui_review.write_section("id", "SPEC", "", "new", column="Spec ready")


async def test_review_read_error_toast_says_lead(sales, monkeypatch):
    def broken(kind, cid):
        raise SystemExit("gone")
    monkeypatch.setattr(tui_review, "load_review", broken)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        await app.push_screen(tui_review.ReviewScreen("spec", CID))
        await settle(pilot)
        assert "could not read the lead: gone" in _notes(app)
