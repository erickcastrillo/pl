"""One worktrees folder per profile ([paths] worktrees_dir): config, validation, and the --add-dir launch flag."""
import re
import shlex
import subprocess
import types

import pytest
import tomlkit

from pl import config as C
from pl import dispatch, harnesses

SID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PL_CONFIG_DIR", raising=False)
    monkeypatch.setattr(harnesses, "_run", lambda argv, **kw: pytest.fail(f"unexpected subprocess {argv}"))
    C.load()
    yield home
    harnesses._CACHE.clear()
    for k, v in saved.items():
        setattr(C, k, v)


@pytest.fixture
def fake_tmux(monkeypatch):
    scripts = []

    def tmux(*args, check=True):
        if args[0] == "send-keys" and "-l" in args:
            scripts.append(open(shlex.split(args[-1])[1]).read())
        return {"new-window": "@7", "list-panes": "%9"}.get(args[0], "")

    monkeypatch.setattr(dispatch, "tmux", tmux)
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: types.SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(dispatch, "update", lambda cid, **f: None)
    monkeypatch.setattr(dispatch.uuid, "uuid4", lambda: SID)
    return scripts


def _work(home):
    w = home / "Code" / "app"
    w.mkdir(parents=True, exist_ok=True)
    return w


def _profile(home, text=""):
    d = home / ".pl-t"
    d.mkdir(exist_ok=True)
    (d / "config.toml").write_text(f'[paths]\nwork_dir = "{_work(home)}"\n{text}')
    C.load(config_dir=str(d))
    harnesses._CACHE.clear()


def _exec(script):
    return shlex.split(next(ln for ln in script.splitlines() if ln.startswith("exec "))[len("exec "):])


def _launch(home, scripts, harness, toml="", account_extra=""):
    """The launch scripts of a stage agent and of a loop under account a of this harness."""
    _profile(home, f'{toml}\n[accounts.a]\nharness = "{harness}"\nconfig_dir = "~"\n{account_extra}\n')
    C.PROMPTS = {"spec": "/spec {id}"}
    dispatch.start_worker({"id": "abc", "title": "t", "metadata": {"profile": "a"}}, "spec", 1, False)
    C.SERVICES = {"rev": {"prompt": "/rev", "profile": "a"}}
    dispatch.ensure_services({}, [], {}, False, None)
    return scripts


# ---------- config ----------

def test_the_default_is_a_sibling_of_the_work_folder(fake_home):
    _profile(fake_home)
    assert C.WORKTREES_DIR == fake_home / "Code" / "app-pl-worktrees"


def test_a_set_folder_wins_and_expands_the_tilde(fake_home):
    _profile(fake_home, 'worktrees_dir = "~/trees"\n')
    assert C.WORKTREES_DIR == fake_home / "trees"


def test_no_profile_has_no_worktrees_folder(fake_home):
    assert C.WORKTREES_DIR is None


def _errors(home, value):
    doc = tomlkit.parse(f'[paths]\nwork_dir = "{_work(home)}"\n')
    doc["paths"]["worktrees_dir"] = value
    return [e for e in C.validate(doc) if "worktrees_dir" in e]


@pytest.mark.parametrize("value", ["{w}", "{w}/trees", "{w}/..", "/", "~"])
def test_a_folder_that_is_the_work_folder_inside_or_above_it_is_refused(fake_home, value):
    errs = _errors(fake_home, value.format(w=_work(fake_home)))
    assert errs and "outside the work folder" in errs[0]


@pytest.mark.parametrize("value", ["~/Code/app-trees", "~/elsewhere/trees", ""])
def test_a_folder_of_its_own_passes_even_before_it_exists(fake_home, value):
    assert _errors(fake_home, value) == []


def test_a_worktrees_dir_that_is_not_text_is_refused(fake_home):
    assert _errors(fake_home, 3)


# ---------- launch ----------

@pytest.mark.parametrize("harness", ["claude", "codex", "antigravity"])
def test_agents_and_loops_get_the_folder_after_the_program_name_and_it_is_made(fake_home, fake_tmux, harness):
    scripts = _launch(fake_home, fake_tmux, harness)
    wt = fake_home / "Code" / "app-pl-worktrees"
    assert len(scripts) == 2
    for script in scripts:
        argv = _exec(script)
        assert argv[1] == f"--add-dir={wt}", argv
        assert argv.count(f"--add-dir={wt}") == 1
        assert f"export PL_WORKTREES_DIR={shlex.quote(str(wt))}" in script
    assert wt.is_dir() and (wt.stat().st_mode & 0o777) == 0o700


def test_the_folder_comes_after_the_model_and_effort_flags(fake_home, fake_tmux):
    for script in _launch(fake_home, fake_tmux, "claude", account_extra='model = "opus"\neffort = "high"'):
        assert _exec(script)[1:6] == ["--model", "opus", "--effort", "high",
                                      f"--add-dir={fake_home / 'Code' / 'app-pl-worktrees'}"]


def test_a_template_that_adds_its_own_folder_is_left_alone(fake_home, fake_tmux):
    toml = '[harnesses.claude]\ninteractive = ["claude", "--add-dir", "/mine", "{prompt}"]\n'
    for script in _launch(fake_home, fake_tmux, "claude", toml):
        assert sum(t.startswith("--add-dir") for t in _exec(script)) == 1


def test_a_custom_harness_gets_no_flag_but_the_folder_and_its_variable(fake_home, fake_tmux):
    toml = '[harnesses.gem]\nbin = "gem"\ninteractive = ["gem", "{prompt}"]\nheadless = ["gem", "{prompt}"]\n'
    for script in _launch(fake_home, fake_tmux, "gem", toml):
        assert not any(t.startswith("--add-dir") for t in _exec(script))
        assert "export PL_WORKTREES_DIR=" in script


def test_a_folder_with_spaces_stays_one_quoted_argument(fake_home, fake_tmux):
    d = fake_home / ".pl-t"
    d.mkdir(exist_ok=True)
    (d / "config.toml").write_text(f'[paths]\nwork_dir = "{_work(fake_home)}"\nworktrees_dir = "~/my trees; id"\n'
                                   '[accounts.a]\nconfig_dir = "~"\n')
    C.load(config_dir=str(d))
    script = harnesses.launch_script(harnesses.get("claude"), "a", "go", SID, "l", plugin=False)
    assert _exec(script)[1] == f"--add-dir={fake_home / 'my trees; id'}"
    assert (fake_home / "my trees; id").is_dir()


def test_a_folder_that_cannot_be_made_is_left_out(fake_home, fake_tmux):
    (fake_home / "file").write_text("")
    d = fake_home / ".pl-t"
    d.mkdir(exist_ok=True)
    (d / "config.toml").write_text(f'[paths]\nwork_dir = "{_work(fake_home)}"\nworktrees_dir = "~/file/trees"\n'
                                   '[accounts.a]\nconfig_dir = "~"\n')
    C.load(config_dir=str(d))
    script = harnesses.launch_script(harnesses.get("claude"), "a", "go", SID, "l", plugin=False)
    assert "--add-dir" not in script and "PL_WORKTREES_DIR" not in script


# ---------- prompts, skills and Settings ----------

def test_a_stage_prompt_can_name_the_folder(fake_home, fake_tmux, monkeypatch):
    monkeypatch.setattr(dispatch.worktrees, "for_run", lambda c: (fake_home / "wt", None))
    _launch(fake_home, fake_tmux, "claude")
    fake_tmux.clear()
    C.PROMPTS = {"run": "/my-run {id} worktrees={worktrees_dir}"}
    dispatch.start_worker({"id": "abc", "title": "t", "metadata": {"profile": "a"}}, "run", 1, False)
    assert _exec(fake_tmux[0])[-1] == f"/my-run abc worktrees={fake_home / 'Code' / 'app-pl-worktrees'}"


def test_the_builtin_run_prompt_is_unchanged_and_its_agent_sees_the_folder(fake_home, fake_tmux, monkeypatch):
    monkeypatch.setattr(dispatch.worktrees, "for_run", lambda c: (fake_home / "wt", None))
    _launch(fake_home, fake_tmux, "claude")
    fake_tmux.clear()
    C.PROMPTS = {"run": C.BUILTIN_PROMPTS["run"]}
    dispatch.start_worker({"id": "abc", "title": "t", "metadata": {"profile": "a"}}, "run", 1, False)
    assert "worktrees=" not in _exec(fake_tmux[0])[-1]
    assert "export PL_WORKTREES_DIR=" in fake_tmux[0]


@pytest.mark.parametrize("name,line", [
    ("pl-review", 'git worktree add --detach "${PL_WORKTREES_DIR:-..}/review-<number>"')])
def test_the_builtin_skills_make_worktrees_in_the_folder(name, line):
    from pl import skills
    text = (skills.BUILTIN_DIR / name / "SKILL.md").read_text()
    assert line in text and int(re.search(r"^pl-builtin-version: (\d+)$", text, re.M).group(1)) >= 3
    assert "git worktree add -b pl/<short-name> ../" not in text and "--detach ../" not in text


async def test_settings_shows_the_folder_and_saves_an_edit(fake_home, monkeypatch):
    from textual.widgets import Input

    from pl.trackers import github
    from pl.tui import settings
    from pl.tui.app import PlApp
    from test_settings import app_data, settle
    monkeypatch.setattr(github, "_gh", lambda args: "github.com\n  ✓ Logged in to github.com account octo (keyring)\n")
    (fake_home / ".claude-a").mkdir()
    _profile(fake_home, '[accounts.a]\nconfig_dir = "~/.claude-a"\n')
    cfg = fake_home / ".pl-t" / "config.toml"
    app = PlApp(snapshot_provider=lambda: app_data([]), interval=3600)
    async with app.run_test(size=(176, 48)) as pilot:
        await settle(pilot)
        app.action_tab("settings")
        await settle(pilot)
        view = app.query_one(settings.SettingsView)
        wid = next(w for w, (p, _, _) in view.fields.items() if p == ("paths", "worktrees_dir"))
        box = app.query_one(f"#{wid}", Input)
        assert box.value == str(fake_home / "Code" / "app-pl-worktrees")
        box.value = str(fake_home / "trees")
        view.action_save()
        await settle(pilot)
    assert tomlkit.parse(cfg.read_text())["paths"]["worktrees_dir"] == str(fake_home / "trees")
    assert C.WORKTREES_DIR == fake_home / "trees"
