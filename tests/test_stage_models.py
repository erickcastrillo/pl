"""pl models set-stage: one stage model key per harness, normal or small-change, written to config.toml."""
import sys

import pytest
import tomlkit

from pl import cli
from pl import config as C
from pl import harnesses


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("PL_CONFIG_DIR", "PL_TMUX_SESSION"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(harnesses, "_run", lambda argv, **kw: pytest.fail(f"unexpected subprocess {argv}"))
    harnesses._MODELS.clear()
    C.load()
    yield tmp_path
    harnesses._CACHE.clear()
    harnesses._MODELS.clear()
    for k, v in saved.items():
        setattr(C, k, v)


def _profile(home, monkeypatch, text):
    d = home / ".pl-t"
    d.mkdir(exist_ok=True)
    (d / "config.toml").write_text(text)
    monkeypatch.setenv("PL_CONFIG_DIR", str(d))
    return d / "config.toml"


def _pl(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["pl", "models", "set-stage", *args])
    cli.main()


ACCOUNTS = '[accounts.a]\nharness = "antigravity"\nconfig_dir = "~"\n[accounts.c]\nharness = "claude"\nconfig_dir = "~"\n'


def test_set_stage_writes_the_models_key_and_prints_it(fake_home, monkeypatch, capsys):
    cfg = _profile(fake_home, monkeypatch, f'# my notes\n{ACCOUNTS}[stages.plan]\nprompt = "/p"\n')
    _pl(monkeypatch, "plan", "antigravity", "claude-opus-5-5-high")
    doc = tomlkit.parse(cfg.read_text())
    assert doc["stages"]["plan"]["models"]["antigravity"] == "claude-opus-5-5-high"
    assert doc["stages"]["plan"]["prompt"] == "/p"
    assert cfg.read_text().startswith("# my notes\n")
    assert 'stages.plan.models.antigravity = "claude-opus-5-5-high"' in capsys.readouterr().out


def test_small_writes_under_small_models_and_keeps_its_limits(fake_home, monkeypatch, capsys):
    cfg = _profile(fake_home, monkeypatch, f"{ACCOUNTS}[stages.run.small]\nmax_lines = 60\nmax_files = 3\n")
    _pl(monkeypatch, "run", "claude", "haiku", "--small")
    small = tomlkit.parse(cfg.read_text())["stages"]["run"]["small"]
    assert small["models"]["claude"] == "haiku"
    assert (small["max_lines"], small["max_files"]) == (60, 3)
    assert capsys.readouterr().err == ""


def test_small_into_an_inline_models_table(fake_home, monkeypatch):
    cfg = _profile(fake_home, monkeypatch,
                   f'{ACCOUNTS}[stages.run.small]\nmax_lines = 60\nmax_files = 3\nmodels = {{claude = "haiku"}}\n')
    _pl(monkeypatch, "run", "antigravity", "gemini-3.8-flash-low", "--small")
    assert tomlkit.parse(cfg.read_text())["stages"]["run"]["small"]["models"] == {
        "claude": "haiku", "antigravity": "gemini-3.8-flash-low"}


def test_small_without_limits_is_refused_naming_the_limits(fake_home, monkeypatch, capsys):
    cfg = _profile(fake_home, monkeypatch, ACCOUNTS)
    before = cfg.read_bytes()
    with pytest.raises(SystemExit):
        _pl(monkeypatch, "run", "claude", "haiku", "--small")
    err = capsys.readouterr().err
    assert "max_lines" in err and "max_files" in err
    assert cfg.read_bytes() == before


def test_empty_model_removes_the_key_and_an_emptied_table(fake_home, monkeypatch, capsys):
    cfg = _profile(fake_home, monkeypatch,
                   f'{ACCOUNTS}[stages.plan]\nprompt = "/p"\n[stages.plan.models]\nantigravity = "claude-opus-5-5"\nclaude = "opus"\n')
    _pl(monkeypatch, "plan", "antigravity", "")
    assert dict(tomlkit.parse(cfg.read_text())["stages"]["plan"]["models"]) == {"claude": "opus"}
    _pl(monkeypatch, "plan", "claude", "")
    plan = tomlkit.parse(cfg.read_text())["stages"]["plan"]
    assert "models" not in plan and plan["prompt"] == "/p"
    assert "removed stages.plan.models.claude" in capsys.readouterr().out


def test_removing_a_missing_key_says_so_and_does_not_write(fake_home, monkeypatch, capsys):
    cfg = _profile(fake_home, monkeypatch, ACCOUNTS)
    before, mtime = cfg.read_bytes(), cfg.stat().st_mtime_ns
    _pl(monkeypatch, "plan", "claude", "")
    assert "nothing to remove" in capsys.readouterr().out
    assert cfg.read_bytes() == before and cfg.stat().st_mtime_ns == mtime


@pytest.mark.parametrize("args,expect", [
    (("deploy", "claude", "opus"), "spec, design, plan, run"),
    (("plan", "vim", "opus"), "unknown harness 'vim'"),
    (("plan", "antigravity", "opus"), "not saved"),
    (("plan", "claude", "bad id"), "not saved"),
])
def test_refusals_name_the_problem_and_leave_the_file_alone(fake_home, monkeypatch, capsys, args, expect):
    cfg = _profile(fake_home, monkeypatch, ACCOUNTS)
    before = cfg.read_bytes()
    with pytest.raises(SystemExit) as e:
        _pl(monkeypatch, *args)
    assert e.value.code
    assert expect in capsys.readouterr().err
    assert cfg.read_bytes() == before


def test_a_harness_that_takes_no_model_is_refused(fake_home, monkeypatch, capsys):
    cfg = _profile(fake_home, monkeypatch, ACCOUNTS)
    before = cfg.read_bytes()
    monkeypatch.setattr(harnesses, "MODEL_FLAGS", {k: v for k, v in harnesses.MODEL_FLAGS.items() if k != "codex"})
    with pytest.raises(SystemExit):
        _pl(monkeypatch, "plan", "codex", "gpt-5")
    assert "takes no model" in capsys.readouterr().err
    assert cfg.read_bytes() == before


def test_no_profile_refuses_with_run_pl_setup(fake_home, monkeypatch, capsys):
    with pytest.raises(SystemExit):
        _pl(monkeypatch, "plan", "claude", "opus")
    assert "pl setup" in capsys.readouterr().err
    assert not list(fake_home.glob(".pl-*/config.toml"))
