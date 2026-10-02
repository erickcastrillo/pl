"""WP2: profile config layer: a profile loads <dir>/config.toml and moves state. WP14: no profile = neutral defaults."""
import stat
import tomllib
import sys

import pytest

from pl import __version__, cli
from pl import config as C


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("PL_CONFIG_DIR", "PL_TMUX_SESSION"):
        monkeypatch.delenv(var, raising=False)
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


def _write(home, name, text):
    d = home / f".pl-{name}"
    d.mkdir()
    (d / "config.toml").write_text(text)
    return d


def test_no_profile_leaves_neutral_defaults(fake_home):
    C.load()
    assert C.TMUX_SESSION == "pl" and C.BOARD is None and C.TRACKER == {}
    assert C.LOCK_FILE == C.STATE_DIR / "pl-dispatch.lock" and C.STATE_FILE == C.STATE_DIR / "pl-dispatch.json"
    assert C.PROFILE_NAME is None and C.CONFIG_DIR is None and C.path() is None
    assert not (fake_home / ".claude-attention").exists()


def test_profile_applies_config_and_moves_state(fake_home):
    d = _write(fake_home, "work", '[tracker]\nboard_id = "b-123"\n[accounts.a]\nconfig_dir = "~/x"\n')
    (d / "state").mkdir(mode=0o755)
    (d / "state").chmod(0o755)
    C.load("work")
    assert C.PROFILE_NAME == "work" and C.CONFIG_DIR == d and C.path() == d / "config.toml"
    assert C.BOARD == "b-123" and C.TRACKER["board_id"] == "b-123"
    assert C.PROFILES == {"a": fake_home / "x"}
    state = fake_home / ".pl-work" / "state"
    assert C.STATE_DIR == state and C.ATTN == state
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert C.STATE_FILE == state / "pl-dispatch.json" and C.LOCK_FILE == state / "pl-dispatch.lock"
    assert C.TMUX_SESSION == "pl-work"


def test_profile_flag_beats_env_folder(fake_home, monkeypatch):
    d = _write(fake_home, "work", "")
    _write(fake_home, "personal", "")
    monkeypatch.setenv("PL_CONFIG_DIR", str(fake_home / ".pl-personal"))
    C.load("work")
    assert C.CONFIG_DIR == d and C.PROFILE_NAME == "work" and C.TMUX_SESSION == "pl-work"
    assert C.STATE_DIR == d / "state"


def test_env_folder_used_without_flag(fake_home, monkeypatch):
    _write(fake_home, "personal", "")
    monkeypatch.setenv("PL_CONFIG_DIR", str(fake_home / ".pl-personal"))
    C.load()
    assert C.PROFILE_NAME == "personal" and C.TMUX_SESSION == "pl-personal"


def test_unsafe_session_or_folder_name_refused(fake_home, monkeypatch):
    _write(fake_home, "work", "tmux_session = \"x'; touch /tmp/pwn; '\"\n")
    with pytest.raises(SystemExit, match="touch"):
        C.load("work")
    _write(fake_home, "num", "tmux_session = 5\n")
    with pytest.raises(SystemExit, match="bad tmux_session"):
        C.load("num")
    bad = fake_home / "Bad Name"
    bad.mkdir()
    monkeypatch.setenv("PL_CONFIG_DIR", str(bad))
    with pytest.raises(SystemExit, match="Bad Name"):
        C.load()


def test_missing_profile_refused_and_not_created(fake_home):
    with pytest.raises(SystemExit, match='no profile "typo"'):
        C.load("typo")
    assert not (fake_home / ".pl-typo").exists()
    (fake_home / ".pl-empty").mkdir()
    with pytest.raises(SystemExit, match='no profile "empty"'):
        C.load("empty")


def test_intake_columns_string_becomes_one_item_list(fake_home):
    _write(fake_home, "work", '[intake]\ncolumns = "Triage"\n')
    C.load("work")
    assert C.PRODUCT_INTAKE_COLUMNS == ["Triage"]


def test_two_loads_leave_nothing_from_the_first(fake_home):
    _write(fake_home, "work", 'tmux_session = "w"\n[tracker]\nboard_id = "b-work"\n[accounts.a]\nconfig_dir = "~/x"\n'
                              '[gates]\nspec = true\n')
    _write(fake_home, "personal", "")
    C.load("work")
    C.load("personal")
    assert C.TMUX_SESSION == "pl-personal"
    assert C.BOARD is None
    assert "a" not in C.PROFILES and C.GATES == {"spec": False}
    assert C.STATE_DIR == fake_home / ".pl-personal" / "state"


@pytest.mark.parametrize("bad", ["../x", "A", ""])
def test_bad_profile_names_refused(bad):
    with pytest.raises(SystemExit, match="bad profile name"):
        C.load(bad)


def test_toml_syntax_error_names_file_and_line(fake_home):
    d = _write(fake_home, "work", 'tmux_session = "ok"\n[tracker\n')
    with pytest.raises(SystemExit) as e:
        C.load("work")
    assert str(d / "config.toml") in str(e.value) and "line 2" in str(e.value)


def test_cli_top_level_profile_and_account_flag(fake_home, monkeypatch):
    _write(fake_home, "work", '[accounts.a]\nconfig_dir = "~/x"\n')
    seen = {}
    monkeypatch.setattr(cli, "cmd_idea", lambda a: seen.update(vars(a)))
    monkeypatch.setattr(sys, "argv", ["pl", "--profile", "work", "idea", "--account", "a", "x"])
    cli.main()
    assert seen["account"] == "a" and seen["text"] == "x"
    assert C.PROFILE_NAME == "work"


def test_empty_config_dir_env_means_no_profile(monkeypatch):
    monkeypatch.setenv("PL_CONFIG_DIR", "")
    C.load()
    assert C.PROFILE_NAME is None and C.TMUX_SESSION == "pl"


NO_PROFILE = "pl: no profile yet: run pl setup to create one (or pick one with pl --profile NAME)"


def test_without_a_profile_commands_refuse_but_help_and_profiles_work(fake_home, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["pl", "list"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert str(e.value) == NO_PROFILE
    monkeypatch.setattr(sys, "argv", ["pl", "--help"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 0 and "usage: pl" in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", ["pl", "--version"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 0 and capsys.readouterr().out.strip() == f"pl {__version__}"
    monkeypatch.setattr(sys, "argv", ["pl", "profiles"])
    cli.main()
    assert "pl profiles new" in capsys.readouterr().out
    _write(fake_home, "work", "")
    monkeypatch.setattr(sys, "argv", ["pl", "--profile", "work", "pause"])
    cli.main()
    assert "paused" in capsys.readouterr().out


def test_defaults_hold_no_company_values_and_missing_required_keys_name_the_key(fake_home):
    _write(fake_home, "bare", "")
    C.load("bare")
    assert C.PROFILES == {} and C.SERVICES == {} and not any(C.PROMPTS.values())
    assert C.ATTENTION is None and C.CODE_HOST == {"owner": None, "labels": {}}
    from pl import trackers
    trackers.reset()
    with pytest.raises(SystemExit, match=r"\[tracker\] type"):
        trackers.get("tracker")


def test_gh_config_dir_expands_home_and_resolves_against_the_profile(fake_home):
    d = _write(fake_home, "work", "")
    C.load("work")
    assert getattr(C, "GH_CONFIG_DIR", "missing") is None        # unset: gh uses its usual sign-in
    (d / "config.toml").write_text('[code_host]\ngh_config_dir = "gh"\n')
    C.load("work")
    assert C.GH_CONFIG_DIR == d / "gh"
    (d / "config.toml").write_text('[code_host]\ngh_config_dir = "~/.config/gh-work"\n')
    C.load("work")
    assert C.GH_CONFIG_DIR == fake_home / ".config" / "gh-work"


def test_validate_rejects_a_non_string_gh_config_dir():
    import tomlkit
    assert any("gh_config_dir" in e for e in C.validate(tomlkit.parse("[code_host]\ngh_config_dir = 3\n")))
    assert C.validate(tomlkit.parse('[code_host]\ngh_config_dir = "~/gh"\n')) == []


def test_validate_max_card_chars_is_a_whole_number_of_at_least_1000():
    import tomlkit
    for bad in ("999", '"40000"', "true", "1500.5"):
        errs = C.validate(tomlkit.parse(f"[tracker]\nmax_card_chars = {bad}\n"))
        assert any("tracker.max_card_chars" in e for e in errs), bad
    assert C.validate(tomlkit.parse("[tracker]\nmax_card_chars = 1000\n")) == []


def test_user_login_is_read_from_the_user_table(fake_home):
    _write(fake_home, "work", '[user]\nlogin = "octo"\n')
    C.load("work")
    assert C.USER_LOGIN == "octo"
    C.load()
    assert C.USER_LOGIN is None


# ---------- WP36: a github-project profile gets the issue intake and an auto-review loop by default ----------

GH_PROFILE = """[tracker]
type = "github-project"
owner = "acme"
number = 3
repo = "acme/app"
[accounts.first]
config_dir = "~/a"
[accounts.second]
config_dir = "~/b"
[code_host.labels]
review = "pl:auto-review"
ready = "pl:ready-for-review"
rework = "pl:needs-rework"
failed = "pl:review-failed"
"""


def test_github_project_profile_turns_both_defaults_on(fake_home):
    _write(fake_home, "gh", GH_PROFILE)
    C.load("gh")
    assert C.ISSUE_INTAKE == {"repo": "acme/app", "start_label": "pl:start"}
    loop = C.SERVICES["auto-review"]
    assert loop["profile"] == "first" and loop["builtin"] is True
    for word in ("acme/app", "pl:auto-review", "pl:ready-for-review", "pl:needs-rework", "pl:review-failed"):
        assert word in loop["prompt"]


def test_either_default_can_be_turned_off_and_an_explicit_setting_wins(fake_home):
    d = _write(fake_home, "gh", GH_PROFILE + "[intake]\nenabled = false\n[loops.auto-review]\nenabled = false\n")
    assert not [e for e in C.validate(tomllib.loads((d / "config.toml").read_text())) if "loops" in e]
    C.load("gh")
    assert C.ISSUE_INTAKE is None and "auto-review" not in C.SERVICES
    (d / "config.toml").write_text(GH_PROFILE + '[intake]\ntype = "mcp"\n[loops.auto-review]\nprompt = "mine"\n')
    C.load("gh")
    assert C.ISSUE_INTAKE is None and C.SERVICES["auto-review"] == {"prompt": "mine", "profile": None}


def test_other_trackers_get_neither_default(fake_home):
    _write(fake_home, "is", '[tracker]\ntype = "github-issues"\nrepo = "acme/app"\n')
    C.load("is")
    assert C.ISSUE_INTAKE is None and C.SERVICES == {}


def test_builtin_review_prompt_keeps_every_outcome_visible_to_pl(fake_home):
    """pl lists only PRs with `ready`; `rework` means problems found; `failed` waits for a person to add `review` back.
    The rules live in the built-in pl-review skill; the prompt hands it the repo and this profile's labels."""
    from pl import skills
    _write(fake_home, "gh", GH_PROFILE)
    C.load("gh")
    p = C.SERVICES["auto-review"]["prompt"]
    assert p == ("/loop 30m /pl-review repo=acme/app review=pl:auto-review ready=pl:ready-for-review "
                 "rework=pl:needs-rework failed=pl:review-failed")
    rules = (skills.BUILTIN_DIR / "pl-review" / "SKILL.md").read_text()
    assert "| Review passes | remove `<review>`, add `<ready>` |" in rules
    assert ("| Review finds problems | post them as one PR comment in plain words, remove `<review>`, "
            "add both `<ready>` and `<rework>` |") in rules
    assert ("| Review cannot run | comment why, remove `<review>`, add `<failed>`; "
            "a person decides, then adds `<review>` back |") in rules
