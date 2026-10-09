"""Model and reasoning effort per harness account, effort per stage: config, launch argv, model lists, Settings."""
import json
import shlex
import subprocess
import types

import pytest
import tomlkit
from textual.widgets import Select

from pl import config as C
from pl import dispatch, harnesses
from pl.trackers import github
from pl.tui import settings

SID = "11111111-2222-3333-4444-555555555555"
REAL_LISTS = dict(harnesses.MODEL_LIST_ARGV)   # conftest empties it for every test; these tests fake _run instead


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("PL_CONFIG_DIR", "PL_TMUX_SESSION"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(github, "_gh", lambda args: "github.com\n  ✓ Logged in to github.com account octo (keyring)\n")
    monkeypatch.setattr(harnesses, "_run", lambda argv, **kw: pytest.fail(f"unexpected subprocess {argv}"))
    monkeypatch.setattr(harnesses, "MODEL_LIST_ARGV", REAL_LISTS)
    harnesses._MODELS.clear()
    C.load()
    yield tmp_path
    harnesses._CACHE.clear()
    harnesses._MODELS.clear()
    for k, v in saved.items():
        setattr(C, k, v)


@pytest.fixture
def fake_tmux(monkeypatch):
    sent = []

    def tmux(*args, check=True):
        if args[0] == "send-keys" and "-l" in args:
            path = shlex.split(args[-1])[1]
            sent.append(next(ln[len("exec "):] for ln in open(path).read().splitlines() if ln.startswith("exec ")))
        return {"new-window": "@7", "list-panes": "%9"}.get(args[0], "")

    monkeypatch.setattr(dispatch, "tmux", tmux)
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: types.SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(dispatch, "update", lambda cid, **f: None)
    monkeypatch.setattr(dispatch.uuid, "uuid4", lambda: SID)
    return sent


def _profile(home, text):
    d = home / ".pl-t"
    d.mkdir(exist_ok=True)
    (d / "config.toml").write_text(text)
    C.load(config_dir=str(d))
    harnesses._CACHE.clear()
    return d / "config.toml"


def _launch(home, fake_tmux, harness, account_extra="", toml="", stage="spec"):
    """The argv of a stage agent and of a loop started under account a of this harness."""
    _profile(home, f'[accounts.a]\nharness = "{harness}"\nconfig_dir = "~"\n{account_extra}\n{toml}')
    C.PROMPTS = {stage: "/spec {id}"}
    dispatch.start_worker({"id": "abc", "title": "t", "metadata": {"profile": "a"}}, stage, 1, False)
    C.SERVICES = {"rev": {"prompt": "/rev", "profile": "a"}}
    dispatch.ensure_services({}, [], {}, False, None)
    return [shlex.split(s) for s in fake_tmux]


# ---------- launch ----------

@pytest.mark.parametrize("harness,model_flag,effort_flag", [
    ("claude", ["--model", "opus"], ["--effort", "high"]),
    ("antigravity", ["--model", "opus"], ["--effort", "high"]),
    ("codex", ["-c", 'model="opus"'], ["-c", 'model_reasoning_effort="high"'])])
def test_agents_and_loops_get_the_accounts_model_and_effort_after_the_program_name(
        fake_home, fake_tmux, harness, model_flag, effort_flag):
    for argv in _launch(fake_home, fake_tmux, harness, 'model = "opus"\neffort = "high"'):
        assert argv[1:5] == model_flag + effort_flag, argv


def test_nothing_is_added_when_the_account_sets_neither(fake_home, fake_tmux):
    for argv in _launch(fake_home, fake_tmux, "claude"):
        assert "--model" not in argv and "--effort" not in argv


def test_stage_effort_beats_the_accounts_and_loops_keep_the_accounts(fake_home, fake_tmux):
    agent, loop = _launch(fake_home, fake_tmux, "claude", 'effort = "low"', '[stages.spec]\neffort = "max"\n')
    assert agent[agent.index("--effort") + 1] == "max"
    assert loop[loop.index("--effort") + 1] == "low"


def test_a_template_that_sets_its_own_model_or_effort_is_left_alone(fake_home, fake_tmux):
    toml = '[harnesses.claude]\ninteractive = ["claude", "--model", "sonnet", "--effort=low", "{prompt}"]\n'
    for argv in _launch(fake_home, fake_tmux, "claude", 'model = "opus"\neffort = "high"', toml):
        assert argv.count("--model") == 1 and "opus" not in argv and "high" not in argv, argv


def test_a_user_agy_template_gets_the_flags_after_the_program_name_once(fake_home, fake_tmux):
    toml = ('[harnesses.antigravity]\ninteractive = ["agy", "--dangerously-skip-permissions", "-i", "{prompt}"]\n'
            'headless = ["agy", "--dangerously-skip-permissions", "-p", "{prompt}"]\n')
    for argv in _launch(fake_home, fake_tmux, "antigravity", 'model = "gemini-3.1-pro-high"\neffort = "xhigh"', toml):
        assert argv[:6] == ["agy", "--model", "gemini-3.1-pro-high", "--effort", "xhigh",
                            "--dangerously-skip-permissions"], argv
        assert argv.count("--model") == 1 and argv.count("--effort") == 1


def test_a_prompt_that_mentions_model_does_not_count_as_a_model_flag(fake_home):
    _profile(fake_home, '[accounts.a]\nharness = "codex"\nconfig_dir = "~"\nmodel = "gpt-5.5"\n')
    argv, _ = harnesses.headless_argv(harnesses.get("codex"), "a", "model=x --model y")
    assert argv[:4] == ["codex", "-c", 'model="gpt-5.5"', "exec"]


def test_headless_runs_get_the_model_and_the_stage_effort(fake_home):
    _profile(fake_home, '[accounts.a]\nharness = "antigravity"\nconfig_dir = "~"\nmodel = "m1"\n'
                        '[stages.plan]\neffort = "medium"\n')
    argv, _ = harnesses.headless_argv(harnesses.get("antigravity"), "a", "hi", stage="plan")
    assert argv == ["agy", "--model", "m1", "--effort", "medium", "-p", "hi"]


def test_a_resumed_agent_keeps_its_model(fake_home):
    _profile(fake_home, '[accounts.a]\nharness = "claude"\nconfig_dir = "~"\nmodel = "sonnet"\n')
    script = harnesses.launch_script(harnesses.get("claude"), "a", "go", SID, "l", resume=True, plugin=False)
    argv = shlex.split(script.splitlines()[-1][len("exec "):])
    assert argv[:4] == ["claude", "--model", "sonnet", "--resume"]


def test_a_stage_harness_other_than_the_accounts_takes_no_model(fake_home):
    _profile(fake_home, '[accounts.a]\nharness = "claude"\nconfig_dir = "~"\nmodel = "opus"\neffort = "high"\n')
    argv, _ = harnesses.headless_argv(harnesses.get("codex"), "a", "hi")
    assert argv[:3] == ["codex", "-c", 'model_reasoning_effort="high"']


def test_a_custom_harness_takes_neither(fake_home):
    _profile(fake_home, '[accounts.a]\nharness = "gem"\nconfig_dir = "~"\n'
                        '[harnesses.gem]\nbin = "gem"\ninteractive = ["gem", "{prompt}"]\nheadless = ["gem", "{prompt}"]\n')
    C.ACCOUNTS["a"]["model"], C.ACCOUNTS["a"]["effort"] = "x", "high"
    argv, _ = harnesses.headless_argv(harnesses.get("gem"), "a", "hi")
    assert argv == ["gem", "hi"]


def test_the_model_flags_never_trip_the_permission_check():
    for flags in (*harnesses.MODEL_FLAGS.values(), *harnesses.EFFORT_FLAGS.values()):
        assert not any(harnesses.PERMISSION_FLAG_RE.search(t) for t in flags), flags


# ---------- model lists ----------

AGY_OUT = ("Fetching available models...\ngemini-3.1-pro-high\tGemini 3.1 Pro (High)\n"
           "claude-opus-5-5-low\tClaude Opus 5.5 (Low)\n")
CODEX_OUT = json.dumps({"models": [{"slug": "gpt-5.5", "visibility": "list"}, {"slug": "hidden", "visibility": "hide"},
                                   {"slug": "gpt-5.6-luna", "visibility": "list"}]})


def test_claude_lists_its_aliases_and_full_ids_without_a_subprocess():
    assert harnesses.models("claude") == ["opus", "sonnet", "haiku", "fable", "claude-opus-5-5", "claude-sonnet-5-5",
                                          "claude-haiku-5-5", "claude-fable-5-1"]


def test_agy_models_come_from_its_cli_once_with_a_timeout(monkeypatch):
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw.get("timeout")))
        return types.SimpleNamespace(returncode=0, stdout=AGY_OUT, stderr="")
    monkeypatch.setattr(harnesses, "_run", run)
    assert harnesses.models("antigravity") == ["gemini-3.1-pro-high", "claude-opus-5-5-low"]
    assert harnesses.models("antigravity") == ["gemini-3.1-pro-high", "claude-opus-5-5-low"]
    assert calls == [(["agy", "models"], harnesses.MODEL_LIST_TIMEOUT)]


def test_codex_models_come_from_its_bundled_catalog(monkeypatch):
    monkeypatch.setattr(harnesses, "_run", lambda argv, **kw: types.SimpleNamespace(
        returncode=0, stdout=CODEX_OUT, stderr="") if argv == ["codex", "debug", "models", "--bundled"] else None)
    assert harnesses.models("codex") == ["gpt-5.5", "gpt-5.6-luna"]


@pytest.mark.parametrize("result", [types.SimpleNamespace(returncode=1, stdout="", stderr="no"),
                                    subprocess.TimeoutExpired("agy", 1), FileNotFoundError("agy"),
                                    types.SimpleNamespace(returncode=0, stdout="not json", stderr="")])
def test_a_failed_model_list_is_empty_so_the_user_types_one(monkeypatch, result):
    def run(argv, **kw):
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(harnesses, "_run", run)
    assert harnesses.models("antigravity") == [] and harnesses.models("codex") == []
    assert harnesses.models("gem") == []


# ---------- validation ----------

def _doc(home, accounts_extra="", rest=""):
    (home / "acct").mkdir(exist_ok=True)
    return tomlkit.parse(f'[accounts.a]\nharness = "claude"\nconfig_dir = "{home}/acct"\n{accounts_extra}\n'
                         f'[accounts.g]\nharness = "antigravity"\nconfig_dir = "{home}/acct"\n{rest}')


def test_valid_model_and_effort_settings_pass(fake_home):
    doc = _doc(fake_home, 'model = "claude-opus-5-5[1m]"\neffort = "xhigh"',
               '[stages.run]\naccounts = ["a", "g"]\neffort = "max"\n[stages.spec]\neffort = "low"\n')
    assert C.validate(doc) == []


@pytest.mark.parametrize("extra,rest,words", [
    ('model = "  "', "", "accounts.a: model must be a model id"),
    ('model = "-x"', "", "accounts.a: model must be a model id"),
    ('model = 3', "", "accounts.a: model must be a model id"),
    ('effort = "huge"', "", "accounts.a: effort 'huge' is not one of low, medium, high, xhigh, max"),
    ("", '[stages.spec]\nmodel = "opus"\n', "stages.spec: model is not a stage setting"),
    ("", '[stages.spec]\neffort = "turbo"\n', "stages.spec: effort 'turbo'"),
    ("", 'model = "opus"\n', "accounts.g: model 'opus' is a claude model"),
])
def test_bad_model_and_effort_settings_are_refused(fake_home, extra, rest, words):
    errs = C.validate(_doc(fake_home, extra, rest))
    assert any(words in e for e in errs), errs


def test_a_harness_without_model_support_reports_both_settings(fake_home):
    doc = _doc(fake_home, rest='[accounts.x]\nharness = "gem"\nconfig_dir = "~"\nmodel = "m"\neffort = "low"\n'
                               '[harnesses.gem]\nbin = "gem"\ninteractive = ["gem"]\nheadless = ["gem"]\n')
    errs = C.validate(doc)
    assert any("accounts.x: harness gem takes no model" in e for e in errs), errs
    assert any("accounts.x: harness gem takes no effort" in e for e in errs), errs


def test_validation_never_runs_a_subprocess(fake_home):
    assert C.validate(_doc(fake_home, rest='model = "anything-new"\n')) == []


# ---------- Settings ----------

CONFIG = """[accounts.main]
harness = "claude"
config_dir = "{acct}"
model = "my-custom-model"

[accounts.agy]
harness = "antigravity"
config_dir = "{acct}"

[stages.run]
accounts = ["main", "agy"]
"""


@pytest.fixture
def work(fake_home):
    acct = fake_home / ".claude-main"
    acct.mkdir()
    return _profile(fake_home, CONFIG.format(acct=acct))


async def _settings(pilot):
    from test_settings import settle
    await settle(pilot)
    pilot.app.action_tab("settings")
    await settle(pilot)
    return pilot.app.query_one(settings.SettingsView)


def _wid(view, path):
    return next(w for w, (p, _, _) in view.fields.items() if p == path)


def _options(view, path):
    return [v for _, v in view.query_one(f"#{_wid(view, path)}", Select)._options if v is not Select.NULL]


async def test_settings_offers_only_each_accounts_harness_models_and_saves_a_pick(work, monkeypatch):
    from pl.tui.app import PlApp
    from test_settings import app_data, settle
    monkeypatch.setattr(harnesses, "_run", lambda argv, **kw: types.SimpleNamespace(returncode=0, stdout=AGY_OUT, stderr=""))
    app = PlApp(snapshot_provider=lambda: app_data([]), interval=3600)
    async with app.run_test(size=(176, 60)) as pilot:
        view = await _settings(pilot)
        main = _options(view, ("accounts", "main", "model"))
        assert main[:8] == harnesses.CLAUDE_MODELS and "my-custom-model" in main and main[-1] == settings.CUSTOM
        assert view.query_one(f"#{_wid(view, ('accounts', 'main', 'model'))}", Select).value == "my-custom-model"
        assert _options(view, ("accounts", "agy", "model")) == ["gemini-3.1-pro-high", "claude-opus-5-5-low",
                                                                 settings.CUSTOM]
        assert _options(view, ("accounts", "main", "effort")) == list(harnesses.EFFORTS)
        assert _options(view, ("stages", "run", "effort")) == list(harnesses.EFFORTS)
        view.query_one(f"#{_wid(view, ('accounts', 'agy', 'model'))}", Select).value = "gemini-3.1-pro-high"
        view.query_one(f"#{_wid(view, ('stages', 'run', 'effort'))}", Select).value = "high"
        view.action_save()
        await settle(pilot)
    doc = tomlkit.parse(work.read_text())
    assert doc["accounts"]["agy"]["model"] == "gemini-3.1-pro-high" and doc["stages"]["run"]["effort"] == "high"
    assert doc["accounts"]["main"]["model"] == "my-custom-model" and "effort" not in doc["accounts"]["main"]


async def test_custom_opens_an_input_and_the_typed_model_is_saved(work, monkeypatch):
    from pl.tui.app import PlApp
    from test_settings import app_data, settle
    monkeypatch.setattr(harnesses, "_run", lambda argv, **kw: types.SimpleNamespace(returncode=1, stdout="", stderr=""))
    app = PlApp(snapshot_provider=lambda: app_data([]), interval=3600)
    async with app.run_test(size=(176, 60)) as pilot:
        view = await _settings(pilot)
        assert _options(view, ("accounts", "agy", "model")) == [settings.CUSTOM]   # agy models failed: type one
        sel = view.query_one(f"#{_wid(view, ('accounts', 'agy', 'model'))}", Select)
        sel.value = settings.CUSTOM
        await settle(pilot)
        assert isinstance(app.screen, settings.ModelScreen)
        await pilot.press(*"gemini-9", "enter")
        await settle(pilot)
        assert sel.value == "gemini-9"
        view.action_save()
        await settle(pilot)
    assert tomlkit.parse(work.read_text())["accounts"]["agy"]["model"] == "gemini-9"


async def test_a_pool_of_harnesses_without_shared_levels_offers_only_the_shared_ones(work, monkeypatch):
    from pl.tui.app import PlApp
    from test_settings import app_data
    monkeypatch.setattr(harnesses, "_run", lambda argv, **kw: types.SimpleNamespace(returncode=1, stdout="", stderr=""))
    monkeypatch.setitem(harnesses.EFFORT_LEVELS, "antigravity", ("low", "high"))
    app = PlApp(snapshot_provider=lambda: app_data([]), interval=3600)
    async with app.run_test(size=(176, 60)) as pilot:
        view = await _settings(pilot)
        assert _options(view, ("stages", "run", "effort")) == ["low", "high"]
