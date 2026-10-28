import json
import sys

import pytest

from pl import cli, e2e, e2e_console as EC
from pl import config as C


def line(**kw):
    return {"test": "t", "outcome": "passed", "reason": "", "where": {}, "screen": "", **kw}


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    (root / "tests" / "e2e_ui").mkdir(parents=True)
    out = tmp_path / "out"
    state = {"calls": [], "lines": [], "pytest_code": 0, "timeout": False}

    def fake_run(argv, cwd=None, env=None, timeout=60, capture=False):
        state["calls"].append({"argv": argv, "cwd": cwd, "env": env, "timeout": timeout})
        if argv[:2] == ["git", "rev-parse"]:
            return 0, f"{root}\n"
        out.mkdir(parents=True, exist_ok=True)
        (out / "results.jsonl").write_text("".join(json.dumps(x) + "\n" for x in state["lines"]))
        return None if state["timeout"] else (state["pytest_code"], "")

    monkeypatch.setattr(EC, "_run", fake_run)
    state["out"], state["root"] = out, root
    return state


def go(env, *extra):
    return EC.cmd(["--out", str(env["out"]), *extra])


def report(env):
    return (env["out"] / "report.md").read_text()


def test_ac1_child_env_drops_real_profile_vars(env, monkeypatch):
    for var in ("PL_CONFIG_DIR", "PL_PROFILE", "PL_WORKTREES_DIR"):
        monkeypatch.setenv(var, "/real")
    go(env)
    call = env["calls"][-1]
    assert call["argv"][:5] == ["uv", "run", "pytest", "-m", "e2e_ui"]
    assert call["cwd"] == str(env["root"])
    assert not {"PL_CONFIG_DIR", "PL_PROFILE", "PL_WORKTREES_DIR"} & call["env"].keys()
    assert call["env"]["PL_E2E_UI_OUT"] == str(env["out"])
    assert call["env"]["PL_NO_UPDATE_CHECK"] == "1"


def test_ac3_report_lists_every_item(env):
    env["lines"] = [line(item="1"), line(item="7", outcome="skipped", reason="not testable here: agents run elsewhere")]
    go(env)
    text = report(env)
    for item in EC.ITEMS:
        assert f"| {item} |" in text
    assert "| 1 | pass" in text
    assert "| 7 | not testable here: agents run elsewhere" in text
    assert "| 2 | fail: no test for this item" in text


def test_ac4_failure_row_names_tab_screen_key_and_screenshot(env):
    env["lines"] = [line(test="test_tab_work", tab="work", outcome="failed", reason="boom",
                         where={"tab": "work", "screen": "CardScreen", "key": "enter"}, screen="screens/test_tab_work.txt")]
    go(env)
    text = report(env)
    assert "| work | fail: boom" in text
    assert "CardScreen" in text and "`enter`" in text and "screens/test_tab_work.txt" in text


def test_ac4_exit_1_on_fail_0_on_none_2_when_console_did_not_start(env):
    env["lines"] = [line(item=i) for i in EC.ITEMS] + [line(tab=t) for t in EC.TABS]
    assert go(env) == 0
    env["lines"][0] = line(item="1", outcome="failed", reason="x")
    assert go(env) == 1
    env["lines"] = [line(item="start", outcome="error", reason="no console")]
    assert go(env) == 2
    env["lines"], env["pytest_code"] = [], 4
    assert go(env) == 2


def test_ac6_timeout_marks_unfinished_items_timed_out_and_exits_1(env):
    env["lines"], env["timeout"] = [line(item="1")], True
    assert go(env, "--timeout", "5") == 1
    assert env["calls"][-1]["timeout"] == 5
    assert "| 2 | fail: timed out" in report(env)


def test_not_a_pl_checkout_exits_2(env, capsys):
    (env["root"] / "tests" / "e2e_ui").rmdir()
    assert go(env) == 2
    assert "run it from a pl checkout (no tests/e2e_ui)" in capsys.readouterr().out
    assert len(env["calls"]) == 1


def test_e2e_console_word_routes_to_console_runner(monkeypatch):
    C.CONFIG_DIR = None
    got = []
    monkeypatch.setattr(EC, "cmd", lambda argv: got.append(("console", argv)) or 0)
    monkeypatch.setattr(e2e, "cmd_e2e", lambda argv: got.append(("e2e", argv)) or 0)
    monkeypatch.setattr(cli.config, "load", lambda *_: pytest.fail("no profile"))
    for argv in (["pl", "e2e", "console", "--timeout", "9"], ["pl", "e2e", "./console"]):
        monkeypatch.setattr(sys, "argv", argv)
        assert cli.main() == 0
    assert got == [("console", ["--timeout", "9"]), ("e2e", ["./console"])]
