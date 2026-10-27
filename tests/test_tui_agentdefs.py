"""Extend → Agents: a read-only view over pl agents list|show --json."""
import json
import subprocess

from rich.text import Text
from textual.widgets import DataTable, Static

from pl.tui import agentdefs
from pl.tui.agentdefs import AgentsView, detail_text, row_cells
from pl.tui.extend import MISSING_STYLE
from tui_wait import view_app, wait_until

AGENTS = {
    "analyst": {"name": "analyst", "source": "built-in", "role": "Reads the data.", "skills": ["pl-read"],
                "connector_roles": ["crm"], "model": "", "approval": [], "pod": "",
                "instructions": "Reads the data.\nSecond line."},
    "my-closer": {"name": "my-closer", "source": "profile", "role": "Closes deals.", "skills": [],
                  "connector_roles": [], "model": "opus", "approval": ["send", "post"], "pod": "sales",
                  "instructions": "Closes deals."},
    "sales-sdr": {"name": "sales-sdr", "source": "profile", "role": "Opens leads.", "skills": ["a", "b"],
                  "connector_roles": ["crm", "mail"], "model": "", "approval": [], "pod": "",
                  "instructions": "Opens leads."},
}


class FakeRunner:
    def __init__(self, agents=AGENTS, problems=None, list_error=None):
        self.agents, self.problems, self.list_error = agents, problems or {}, list_error
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if argv[:3] == ["pl", "agents", "list"]:
            if self.list_error:
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr=self.list_error)
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(list(self.agents.values())), stderr="")
        name = argv[3]
        data = {**self.agents[name], "problems": self.problems.get(name, [])}
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(data), stderr="")


def _app(monkeypatch, runner):
    monkeypatch.setattr(agentdefs, "_run", runner)
    return view_app(AgentsView)


def _rows(app):
    t = app.query_one("#agents-table", DataTable)
    return [[str(c) for c in t.get_row_at(i)] for i in range(t.row_count)]


async def test_table_lists_every_agent_in_cli_order_with_five_columns(monkeypatch):
    app = _app(monkeypatch, FakeRunner())
    async with app.run_test() as pilot:
        await wait_until(pilot, lambda: len(_rows(app)) == 3, "three rows")
        t = app.query_one("#agents-table", DataTable)
        assert [str(c.label) for c in t.columns.values()] == ["name", "source", "role", "skills", "connector roles"]
        assert _rows(app)[0] == ["analyst", "built-in", "Reads the data.", "pl-read", "crm"]
        assert [r[0] for r in _rows(app)] == ["analyst", "my-closer", "sales-sdr"]


async def test_profile_override_appears_once_with_source_profile(monkeypatch):
    app = _app(monkeypatch, FakeRunner({k: AGENTS[k] for k in ("my-closer", "sales-sdr")}))
    async with app.run_test() as pilot:
        await wait_until(pilot, lambda: len(_rows(app)) == 2, "two rows")
        rows = _rows(app)
        assert [r for r in rows if r[0] == "sales-sdr"] == [["sales-sdr", "profile", "Opens leads.", "a, b", "crm, mail"]]
        assert next(r for r in rows if r[0] == "my-closer")[1] == "profile"


async def test_enter_shows_full_definition_and_escape_returns_to_same_row(monkeypatch):
    app = _app(monkeypatch, FakeRunner())
    async with app.run_test() as pilot:
        t = app.query_one("#agents-table", DataTable)
        await wait_until(pilot, lambda: t.row_count == 3, "rows")
        t.focus()
        await pilot.press("down", "enter")
        detail = app.query_one("#agents-detail", Static)
        await wait_until(pilot, lambda: detail.display, "the detail")
        text = str(detail.render())
        assert "Closes deals." in text and "model: opus" in text and "pod: sales" in text
        assert not t.display
        await pilot.press("escape")
        await wait_until(pilot, lambda: t.display, "the table back")
        assert t.cursor_row == 1 and not detail.display


def test_detail_text_shows_stage_default_and_none_for_empty_fields():
    text = detail_text({**AGENTS["analyst"]}).plain
    assert text.startswith("Reads the data.\nSecond line.")
    assert "skills: pl-read" in text and "connector roles: crm" in text
    assert "model: stage default" in text and "approval: none" in text and "pod: none" in text
    assert "problems:" not in text


def test_row_cells_use_warning_style_only_with_problems():
    assert all(c.style == "" for c in row_cells(AGENTS["analyst"], []))
    assert all(c.style == MISSING_STYLE for c in row_cells(AGENTS["analyst"], ["x"]))


async def test_agent_with_problems_is_warning_styled_and_detail_names_each_problem(monkeypatch):
    problems = ["unknown skill: ghost", "unknown connector role: fax"]
    app = _app(monkeypatch, FakeRunner(problems={"my-closer": problems}))
    async with app.run_test() as pilot:
        t = app.query_one("#agents-table", DataTable)
        await wait_until(pilot, lambda: t.row_count == 3 and t.get_row_at(1)[0].style == MISSING_STYLE, "styled row")
        assert t.get_row_at(0)[0].style == ""
        t.focus()
        await pilot.press("down", "enter")
        detail = app.query_one("#agents-detail", Static)
        await wait_until(pilot, lambda: detail.display, "the detail")
        text = str(detail.render())
        assert "problems:" in text and all(p in text for p in problems)
    assert detail_text({**AGENTS["my-closer"], "problems": problems}).spans[-1].style == MISSING_STYLE


async def test_list_failure_shows_pl_error_text_and_hides_table(monkeypatch):
    err = "pl: agents/x/agent.md: bad front matter"
    app = _app(monkeypatch, FakeRunner(list_error=f"noise\n{err}\n"))
    async with app.run_test() as pilot:
        msg = app.query_one("#agents-message", Static)
        await wait_until(pilot, lambda: err in str(msg.render()), "the error")
        assert not app.query_one("#agents-table", DataTable).display


async def test_write_keys_run_nothing_and_write_nothing(monkeypatch, tmp_path):
    runner = FakeRunner()
    app = _app(monkeypatch, runner)
    async with app.run_test() as pilot:
        t = app.query_one("#agents-table", DataTable)
        await wait_until(pilot, lambda: t.row_count == 3, "rows")
        t.focus()
        await pilot.press("n", "e", "d", "u", "enter", "escape")
        await pilot.pause()
    assert all(c[:3] in (["pl", "agents", "list"], ["pl", "agents", "show"]) for c in runner.calls)
    assert list(tmp_path.iterdir()) == []
