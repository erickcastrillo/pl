"""pl idea --dry-run against each real connector: it previews the card and claims nothing was created."""
import sys

import pytest

from pl import cli, commands, events, trackers
from pl import config as C
from pl.trackers.github import GitHubIssues, GitHubProject
from pl.trackers.local import Local
from pl.trackers.mcp import Mcp
from pl.trackers.vikunja import Vikunja

BOARDS = {
    "github-project": 'owner = "acme"\nnumber = 7\nrepo = "acme/app"',
    "github-issues": 'repo = "acme/app"',
    "mcp": 'server = { command = "true" }\nboard_id = "b"\ncard_url = "https://board/{item_id}"',
    "local": "",
    "vikunja": 'url = "http://127.0.0.1:1"\ntoken = "${VIKUNJA_TOKEN}"\nproject_id = 1\nview_id = 2',
}
CLASSES = {"github-project": GitHubProject, "github-issues": GitHubIssues, "mcp": Mcp, "local": Local, "vikunja": Vikunja}
IDS = {"github-project": "acme/app#1", "github-issues": "acme/app#1", "mcp": "m1", "local": "l1", "vikunja": "vikunja#1"}
NEXT_STEPS = ("the dispatcher writes its spec", "assigned to you", "unassigned, waiting", "pipeline board, Inbox")


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("PL_CONFIG_DIR", "PL_LOOP_WRITES", "PL_ASSISTANT", "PL_SOURCE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("VIKUNJA_TOKEN", "secret")
    (tmp_path / ".pl-t").mkdir()
    trackers.reset()
    yield tmp_path
    trackers.set_dry_run(False)
    trackers.reset()
    for k, v in saved.items():
        setattr(C, k, v)


def configure(home, kind, intake=None):
    text = f'[tracker]\ntype = "{kind}"\n{BOARDS[kind]}\n'
    if intake:
        text += f'[intake]\ntype = "{intake}"\n{BOARDS[intake]}\n'
    (home / ".pl-t" / "config.toml").write_text(text)


def pl(monkeypatch, capsys, *argv):
    monkeypatch.setattr(sys, "argv", ["pl", "--profile", "t", *argv])
    try:
        cli.main()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    else:
        code = 0
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def claims_no_card(out):
    assert not any(line.startswith("created ") for line in out.splitlines())
    assert "https://" not in out
    assert not any(step in out for step in NEXT_STEPS)


def files(home):
    return sorted(str(p.relative_to(home)) for p in (home / ".pl-t").rglob("*") if p.is_file())


@pytest.mark.parametrize("kind", BOARDS)
def test_dry_run_exits_0_and_prints_the_preview(home, monkeypatch, capsys, kind):
    configure(home, kind)
    code, out, err = pl(monkeypatch, capsys, "idea", "some text", "--dry-run")
    assert (code, "Traceback" in err) == (0, False), err
    assert any(line.startswith("would create card:") for line in out.splitlines())


@pytest.mark.parametrize("kind", BOARDS)
def test_dry_run_claims_no_card(home, monkeypatch, capsys, kind):
    configure(home, kind)
    claims_no_card(pl(monkeypatch, capsys, "idea", "some text", "--dry-run")[1])


@pytest.mark.parametrize("start", [[], ["--start"]])
@pytest.mark.parametrize("intake", ["vikunja", "github-issues"])
def test_dry_run_with_intake_names_triage(home, monkeypatch, capsys, intake, start):
    configure(home, "local", intake)
    monkeypatch.setattr(commands, "product_lists", lambda: {"Triage": "1"})
    code, out, err = pl(monkeypatch, capsys, "idea", "some text", "--dry-run", *start)
    assert (code, "Traceback" in err) == (0, False), err
    assert any(line.startswith("would create card: column='Triage'") for line in out.splitlines())
    claims_no_card(out)


@pytest.mark.parametrize("kind", BOARDS)
def test_dry_run_creates_nothing_and_writes_no_state(home, monkeypatch, capsys, kind):
    configure(home, kind)

    def forbidden(*a, **kw):
        raise AssertionError("a dry run created a card")
    monkeypatch.setattr(CLASSES[kind], "create", forbidden)
    monkeypatch.setenv("PL_SOURCE", "test")
    before = files(home)
    assert pl(monkeypatch, capsys, "idea", "some text", "--dry-run")[0] == 0
    assert files(home) == before
    assert not any(f.endswith("board.db") for f in files(home))


@pytest.mark.parametrize("argv", [("idea", ""), ("idea", "text", "--doc", "missing.md")], ids=["empty text", "missing doc"])
def test_bad_input_is_the_same_with_and_without_dry_run(home, monkeypatch, capsys, argv):
    configure(home, "local")
    plain = pl(monkeypatch, capsys, *argv)
    dry = pl(monkeypatch, capsys, *argv, "--dry-run")
    assert plain[0] == dry[0] == 2
    assert plain[2] == dry[2]
    assert "Traceback" not in dry[2]


@pytest.mark.parametrize("kind", BOARDS)
def test_real_run_still_reports_the_card(home, monkeypatch, capsys, kind):
    configure(home, kind)
    monkeypatch.setattr(CLASSES[kind], "create", lambda self, column, **kw: {"id": IDS[kind]})
    emitted = []
    monkeypatch.setattr(events, "emit", lambda *a, **kw: emitted.append(a))
    assert pl(monkeypatch, capsys, "idea", "some text", "--dry-run")[0] == 0
    trackers.set_dry_run(False)
    code, out, err = pl(monkeypatch, capsys, "idea", "some text")
    assert code == 0, err
    assert f"created {IDS[kind]}" in out
    assert "pipeline board, Inbox" in out
    assert trackers.get("tracker").url(IDS[kind]) in out
    assert "the dispatcher writes its spec on its next pass" in out
    assert emitted == [("idea_approved", IDS[kind])]
