import pytest
import tomlkit

from pl import config as C


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
    for k, v in saved.items():
        setattr(C, k, v)


def test_sync_settings_load(fake_home):
    toml = fake_home / ".pl-t" / "config.toml"
    toml.write_text('[tracker]\ntype = "local"\n\n[sync]\ngithub = "your-org/projects/5"\n')
    C.load("t")
    assert C.SYNC.get("github") == "your-org/projects/5"


def test_sync_github_bad_value_is_an_error():
    doc = tomlkit.parse('[tracker]\ntype = "local"\n\n[sync]\ngithub = "your-org/5"\n')
    errs = C.validate(doc)
    assert "sync.github must look like owner/projects/N" in errs


def test_sync_github_needs_local_tracker():
    doc = tomlkit.parse('[tracker]\ntype = "github-project"\nowner = "your-org"\nnumber = "5"\n\n[sync]\ngithub = "your-org/projects/5"\n')
    errs = C.validate(doc)
    assert 'sync.github needs [tracker] type = "local"' in errs


def test_sync_git_validation():
    for bad in ('git = ""\n', 'git = 123\n'):
        doc = tomlkit.parse(f"[sync]\n{bad}")
        assert "sync.git must be a folder path" in C.validate(doc)
    ok = tomlkit.parse('[sync]\ngit = "~/pl-backup"\n')
    assert "sync.git must be a folder path" not in C.validate(ok)
