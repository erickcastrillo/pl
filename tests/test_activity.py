"""pl activity and the Activity tab's level filters, over one fixed log."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from pl import activity, cli, trackers
from pl import config as C
from pl.tui import loops
from pl.util import BadInput

NOW = datetime.now(timezone.utc).replace(microsecond=0)


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("PL_CONFIG_DIR", "PL_TMUX_SESSION"):
        monkeypatch.delenv(var, raising=False)
    d = tmp_path / ".pl-t"
    d.mkdir()
    (d / "config.toml").write_text("")
    C.load("t")
    yield tmp_path
    trackers.set_dry_run(False)
    for k, v in saved.items():
        setattr(C, k, v)


def _log(*rows):
    """rows: (minutes ago, kind, extra) written oldest first as the log holds them."""
    C.STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(C.STATE_DIR / "events.jsonl", "w") as f:
        for ago, kind, extra in rows:
            f.write(json.dumps({"ts": (NOW - timedelta(minutes=ago)).isoformat(), "kind": kind, "card": "c1",
                                **extra}) + "\n")


@pytest.fixture
def fixed_log():
    _log((600, "error", {"message": "old boom"}),
         (500, "moved", {"to": "Done"}),
         (400, "held", {"reason": "slow"}),
         (300, "github_budget_gap", {"level": "warning"}),
         (200, "runaway", {"level": "error", "message": "stopped"}),
         (100, "moved", {"to": "Plan for review"}),
         (50, "demo_warning", {"level": "warning", "demo": True, "message": "sample warning"}),
         (40, "demo_error", {"level": "error", "demo": True, "message": "sample error"}),
         (10, "started", {}))


def _run(capsys, *argv):
    a = cli.build_parser().parse_args(["activity", *argv])
    activity.cmd_activity(a)
    return capsys.readouterr().out


def test_no_options_prints_every_entry_newest_first_with_its_level(fixed_log, capsys):
    lines = _run(capsys).splitlines()
    assert len(lines) == 9
    assert lines[0].split()[2:4] == ["info", "started"]
    assert "error" in lines[-1] and "old boom" in lines[-1]
    assert any("held" in ln and "warning" in ln for ln in lines)
    assert [ln for ln in lines if ln.endswith(" demo")].__len__() == 2


def test_level_kind_and_since_must_all_match(fixed_log, capsys):
    assert len(_run(capsys, "--level", "warning").splitlines()) == 3
    assert len(_run(capsys, "--level", "warning", "--kind", "held").splitlines()) == 1
    assert _run(capsys, "--level", "warning", "--kind", "runaway") == ""
    assert len(_run(capsys, "--level", "error", "--since", "250m").splitlines()) == 2
    assert len(_run(capsys, "--since", "90m").splitlines()) == 3
    assert len(_run(capsys, "--since", "1d").splitlines()) == 9
    assert len(_run(capsys, "--since", (NOW - timedelta(days=1)).strftime("%Y-%m-%d")).splitlines()) >= 1


def test_json_prints_one_object_per_line_with_level(fixed_log, capsys):
    rows = [json.loads(ln) for ln in _run(capsys, "--json").splitlines()]
    assert len(rows) == 9 and all("_t" not in r for r in rows)
    assert next(r for r in rows if r["kind"] == "held")["level"] == "warning"
    assert rows[-1]["level"] == "error"


def test_a_bad_level_exits_2_naming_the_choices(fixed_log, capsys):
    with pytest.raises(BadInput, match="pl activity: --level takes info, warning or error, not 'fatal'"):
        _run(capsys, "--level", "fatal")


def test_a_bad_since_exits_2_in_the_standup_style(fixed_log, capsys):
    with pytest.raises(BadInput, match=r"pl activity: --since takes 24h, 90m, 7d, YYYY-MM-DD"):
        _run(capsys, "--since", "soon")


def test_demo_writes_one_entry_per_level_marked_demo(capsys):
    assert "wrote 3 demo entries" in _run(capsys, "demo")
    rows = [json.loads(ln) for ln in (C.STATE_DIR / "events.jsonl").read_text().splitlines()]
    assert sorted(r["level"] for r in rows) == ["error", "info", "warning"]
    assert all(r["demo"] is True for r in rows)


def test_demo_in_dry_run_writes_nothing(capsys):
    trackers.set_dry_run(True)
    assert "dry-run is on; no demo entries written" in _run(capsys, "demo")
    assert not (C.STATE_DIR / "events.jsonl").exists()


def test_demo_clear_removes_demo_entries_and_keeps_the_rest_in_order(fixed_log, capsys):
    path = C.STATE_DIR / "events.jsonl"
    keep = [ln for ln in path.read_text().splitlines() if '"demo"' not in ln]
    assert "removed 2 demo entries" in _run(capsys, "demo", "--clear")
    assert path.read_text().splitlines() == keep


def _events(fixed):
    return loops.read_events(C.STATE_DIR / "events.jsonl")


def test_tab_filters_by_level_and_keeps_the_other_rules(fixed_log):
    evs = _events(fixed_log)
    count = lambda f: sum(loops.matches(e, f) for e in evs)  # noqa: E731
    assert loops.FILTERS == ["all", "errors", "warnings", "needs you", "dispatcher", "loops"]
    assert (count("all"), count("errors"), count("warnings")) == (9, 3, 3)
    assert count("needs you") == 1 and count("dispatcher") == 4 and count("loops") == 0
    old = {"kind": "moved"}
    assert not loops.matches(old, "errors") and not loops.matches(old, "warnings")


async def test_tab_filter_bar_shows_counts_and_level_column_colours(fixed_log):
    from textual.app import App
    from textual.widgets import DataTable, Static

    class Host(App):
        def compose(self):
            yield loops.ActivityView()

    async with Host().run_test() as pilot:
        view = pilot.app.query_one(loops.ActivityView)
        view.show()
        await pilot.pause()
        assert "errors 3" in str(pilot.app.query_one("#activity-filter", Static).render())
        assert "warnings 3" in str(pilot.app.query_one("#activity-filter", Static).render())
        table = pilot.app.query_one(DataTable)
        assert [c.label.plain for c in table.columns.values()] == ["time", "level", "kind", "card", "detail"]
        rows = [table.get_row_at(i) for i in range(table.row_count)]
        by_kind = {r[2].plain.strip(): r for r in rows}
        assert by_kind["runaway"][1].style == "red" and by_kind["held"][1].style == "yellow"
        assert by_kind["started"][1].style == ""
        assert "demo" in by_kind["demo_error demo"][2].plain
