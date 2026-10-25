"""Model names the harness does not offer are refused with their key path; an unreadable list only warns."""
import subprocess
import types

import pytest
import tomlkit

from pl import config as C
from pl import dispatch, harnesses

AGY_OUT = "gemini-3.8-flash-medium\tGemini\nclaude-opus-5-5-high\tOpus\n"
OK = types.SimpleNamespace(returncode=0, stdout=AGY_OUT, stderr="")


@pytest.fixture(autouse=True)
def lists(monkeypatch):
    monkeypatch.setattr(harnesses, "MODEL_LIST_ARGV", {"antigravity": ["agy", "models"]})
    monkeypatch.setattr(harnesses, "_run", lambda argv, **kw: OK)
    harnesses._MODELS.clear()
    dispatch._WARNED_MODELS.clear()
    yield
    harnesses._MODELS.clear()


def _fail_with(monkeypatch, how):
    def run(argv, **kw):
        if how == "missing":
            raise FileNotFoundError("agy")
        if how == "timeout":
            raise subprocess.TimeoutExpired(argv, 1)
        return types.SimpleNamespace(returncode=1 if how == "exit" else 0, stdout="", stderr="")
    monkeypatch.setattr(harnesses, "_run", run)


def _doc(accounts="", rest=""):
    return tomlkit.parse(f'[accounts.main]\nharness = "claude"\nconfig_dir = "~"\n{accounts}\n'
                         f'[accounts.agy]\nharness = "antigravity"\nconfig_dir = "~"\n{rest}')


def test_an_agy_account_model_not_listed_is_refused_with_key_path_and_names():
    doc = tomlkit.parse('[accounts.agy]\nharness = "antigravity"\nconfig_dir = "~"\nmodel = "gemini-3.8-flahs-medium"\n')
    errs, warns = C.check_models(doc)
    assert len(errs) == 1 and warns == []
    for part in ("accounts.agy.model", "gemini-3.8-flahs-medium", "gemini-3.8-flash-medium", "claude-opus-5-5-high"):
        assert part in errs[0]


def test_a_stage_agy_model_not_listed_is_refused_and_a_listed_one_passes():
    errs, _ = C.check_models(_doc(rest='[stages.plan.models]\nantigravity = "nope"\n'))
    assert len(errs) == 1 and "stages.plan.models.antigravity" in errs[0] and "claude-opus-5-5-high" in errs[0]
    assert C.check_models(_doc(rest='[stages.plan.models]\nantigravity = "claude-opus-5-5-high"\n')) == ([], [])


def test_claude_names_are_checked_against_pls_list(monkeypatch):
    monkeypatch.setattr(harnesses, "_run", lambda argv, **kw: pytest.fail("claude needs no CLI"))
    errs, _ = C.check_models(_doc('model = "opsu"', '[stages.run.models]\nclaude = "opsu"\n'))
    assert len(errs) == 2 and "accounts.main.model" in errs[0] and "stages.run.models.claude" in errs[1]
    assert all("opus" in e and "claude-sonnet-5-5" in e for e in errs)
    assert C.check_models(_doc('model = "claude-sonnet-5-5"', '[stages.run.models]\nclaude = "opus"\n')) == ([], [])


def test_small_change_models_are_checked_too():
    errs, _ = C.check_models(_doc(rest='[stages.run.small]\nmax_lines = 60\nmax_files = 3\n'
                                       '[stages.run.small.models]\nclaude = "opsu"\n'))
    assert len(errs) == 1 and "stages.run.small.models.claude" in errs[0]


@pytest.mark.parametrize("how", ["missing", "timeout", "exit", "empty"])
def test_an_unreadable_agy_list_warns_per_key_and_never_refuses(monkeypatch, how):
    _fail_with(monkeypatch, how)
    errs, warns = C.check_models(_doc(rest='model = "whatever"\n[stages.plan.models]\nantigravity = "x"\n'))
    assert errs == [] and len(warns) == 2
    assert "accounts.agy.model" in warns[0] and "stages.plan.models.antigravity" in warns[1]
    assert all("could not be read" in w for w in warns)


def test_each_bad_name_gets_its_own_error():
    errs, _ = C.check_models(_doc('model = "opsu"', 'model = "bad-a"\n[stages.plan.models]\nantigravity = "bad-b"\n'))
    assert len(errs) == 3
    for key, e in zip(("accounts.main.model", "accounts.agy.model", "stages.plan.models.antigravity"), errs):
        assert key in e


def test_codex_and_custom_harnesses_keep_the_syntax_only_check(monkeypatch):
    monkeypatch.setattr(harnesses, "_run", lambda argv, **kw: pytest.fail("no CLI"))
    doc = tomlkit.parse('[accounts.c]\nharness = "codex"\nconfig_dir = "~"\nmodel = "gpt-9"\n')
    assert C.check_models(doc) == ([], [])


def test_no_model_settings_never_runs_agy_models(monkeypatch):
    monkeypatch.setattr(harnesses, "_run", lambda argv, **kw: pytest.fail("agy models ran"))
    assert C.check_models(_doc()) == ([], [])


@pytest.fixture
def profile(tmp_path):
    d = tmp_path / ".pl-t"
    d.mkdir()
    (tmp_path / "acct").mkdir()
    text = f'[accounts.main]\nharness = "claude"\nconfig_dir = "{tmp_path}/acct"\nmodel = "opus"\n'
    (d / "config.toml").write_text(text)
    C.load(config_dir=str(d))
    yield d / "config.toml"
    harnesses._CACHE.clear()


def test_save_refuses_a_bad_name_and_leaves_the_file_untouched(profile):
    before = profile.read_text()
    doc = tomlkit.parse(before)
    doc["accounts"]["main"]["model"] = "opsu"
    with pytest.raises(SystemExit) as e:
        C.save(doc)
    assert "accounts.main.model" in str(e.value) and "opsu" in str(e.value)
    assert profile.read_text() == before


def test_save_returns_the_warnings_and_writes(profile, monkeypatch):
    _fail_with(monkeypatch, "missing")
    doc = tomlkit.parse(profile.read_text())
    doc["accounts"]["agy"] = {"harness": "antigravity", "config_dir": str(profile.parent), "model": "gemini-x"}
    warns = C.save(doc)
    assert len(warns) == 1 and "accounts.agy.model" in warns[0]
    assert "gemini-x" in profile.read_text()


def test_reload_keeps_previous_settings_on_a_bad_model_name(profile, capsys):
    kept = dict(C.ACCOUNTS)
    profile.write_text(profile.read_text().replace('"opus"', '"opsu"'))
    dispatch.reload_config()
    assert C.ACCOUNTS == kept and C.ACCOUNTS["main"].get("model") == "opus"
    out = capsys.readouterr().out
    assert "keeping the previous ones" in out and "accounts.main.model" in out


def test_reload_prints_an_unchecked_warning_once(profile, monkeypatch, capsys):
    _fail_with(monkeypatch, "missing")
    profile.write_text(profile.read_text() + '[accounts.agy]\nharness = "antigravity"\nconfig_dir = "~"\nmodel = "x"\n')
    dispatch.reload_config()
    dispatch.reload_config()
    assert capsys.readouterr().out.count("accounts.agy.model") == 1


def test_dispatch_start_refuses_a_bad_model_name(profile, monkeypatch):
    profile.write_text(profile.read_text().replace('"opus"', '"opsu"'))
    monkeypatch.setattr(dispatch, "dispatch_once", lambda *a, **kw: pytest.fail("a pass ran"))
    with pytest.raises(SystemExit) as e:
        dispatch.cmd_dispatch(types.SimpleNamespace(dry_run=True, interval=None, max_runs=None, max_prep=None,
                                                    no_pull=True))
    assert "not started" in str(e.value) and "accounts.main.model" in str(e.value)
