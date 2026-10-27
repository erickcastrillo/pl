"""Extend > Pod: the pipeline's pod first (skills with their stages, connector roles), then the library."""
import io

import pytest
from rich.console import Console

from pl import config as C
from pl import connectors, pipeline, pods
from pl.tui.app import PlApp
from pl.tui.extend import pod_rows
from test_tui_views import Provider, settle


def stages(**by_name):
    return {"stages": {n: {"skill": s} for n, s in by_name.items()}}


def rows_by_name(rows):
    return {name: (detail, missing) for name, detail, missing in rows}


def sales_pod(monkeypatch, roles=("contacts", "email")):
    monkeypatch.setattr(C, "PIPELINE", stages(proposal="pl-sales-proposal", **{"follow-up": "pl-sales-follow-up"}))
    monkeypatch.setattr(C, "POD", "sales")
    monkeypatch.setattr(C, "PODS", {"sales": {"skills": ["pl-sales-proposal", "pl-sales-follow-up"]}})
    real = pods.current
    monkeypatch.setattr(pods, "current", lambda: {**real(), "roles": list(roles)})


@pytest.fixture(autouse=True)
def no_connectors(monkeypatch):
    monkeypatch.setattr(connectors, "load", lambda: {})


def test_engineering_pod_pairs_each_skill_with_its_stage(monkeypatch):
    monkeypatch.setattr(C, "PIPELINE", pipeline.engineering())
    monkeypatch.setattr(C, "POD", None)
    title, rows = pod_rows()
    got = rows_by_name(rows)
    assert title == "Pod: engineering"
    assert got["pl-spec"] == ("spec", False)
    assert got["pl-plan"] == ("plan", False)
    assert got["pl-run"] == ("run", False)


def test_a_pod_skill_no_stage_runs_says_no_stage(monkeypatch):
    monkeypatch.setattr(C, "PIPELINE", pipeline.engineering())
    monkeypatch.setattr(C, "POD", None)
    assert rows_by_name(pod_rows()[1])["pl-review"] == ("no stage", False)


def test_a_sales_pod_lists_only_its_skills_and_roles(monkeypatch):
    sales_pod(monkeypatch)
    monkeypatch.setattr(connectors, "load", lambda: {"crm": {"role": ["contacts"]}})
    title, rows = pod_rows()
    got = rows_by_name(rows)
    assert title == "Pod: sales"
    assert got["pl-sales-proposal"] == ("proposal", False)
    assert got["pl-sales-follow-up"] == ("follow-up", False)
    assert got["contacts"] == ("crm", False)
    assert "pl-spec" not in got


def test_a_stage_skill_or_role_outside_the_pod_says_missing(monkeypatch):
    sales_pod(monkeypatch)
    monkeypatch.setattr(C, "PIPELINE", stages(triage="pl-support-triage", proposal="pl-sales-proposal"))
    got = rows_by_name(pod_rows()[1])
    assert got["pl-support-triage"] == ("triage: missing", True)
    assert got["email"] == ("missing", True)


def test_a_connector_file_that_cannot_be_read_counts_as_no_connectors(monkeypatch):
    sales_pod(monkeypatch)

    def bad():
        raise ValueError("bad toml")
    monkeypatch.setattr(connectors, "load", bad)
    assert rows_by_name(pod_rows()[1])["contacts"] == ("missing", True)


def test_a_pod_not_in_pods_shows_the_pl_error(monkeypatch):
    monkeypatch.setattr(C, "POD", "nope")
    monkeypatch.setattr(C, "PODS", {})
    title, rows = pod_rows()
    assert rows == [("pl: [pipeline] pod names 'nope', which is not in [pods]", "", True)]


def screen_text(app):
    console = Console(width=app.size.width, height=app.size.height, file=io.StringIO(), record=True,
                      force_terminal=True, color_system="truecolor", legacy_windows=False, safe_box=False)
    console.print(app.screen._compositor.render_update(full=True))
    return console.export_text()


async def test_extend_shows_the_pod_section_before_the_library(monkeypatch):
    monkeypatch.setattr(C, "PIPELINE", pipeline.engineering())
    monkeypatch.setattr(C, "POD", None)
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        await pilot.press("6")
        await settle(pilot)
        assert app.active_tab == "extend"
        text = screen_text(app)
        assert text.index("Pod: engineering") < text.index("Library")
        assert "pl-plan" in text


async def test_a_bad_pod_still_shows_the_library(monkeypatch):
    monkeypatch.setattr(C, "POD", "nope")
    monkeypatch.setattr(C, "PODS", {})
    app = PlApp(snapshot_provider=Provider())
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        app.action_tab("extend")
        await settle(pilot)
        text = screen_text(app)
        assert "not in [pods]" in text and "Library" in text
