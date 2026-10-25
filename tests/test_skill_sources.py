import json
import os
import shutil

import pytest

from pl import config as C
from pl import skill_sources as S

SHA = "a" * 40


def test_parse_accepts_the_short_and_url_forms():
    assert S.parse("github:acme/kit") == {"owner": "acme", "repo": "kit", "path": "", "ref": ""}
    assert S.parse("github:acme/kit/skills/review@v1.2") == {"owner": "acme", "repo": "kit", "path": "skills/review", "ref": "v1.2"}
    assert S.parse("https://github.com/acme/kit") == {"owner": "acme", "repo": "kit", "path": "", "ref": ""}
    assert S.parse("https://github.com/acme/kit/tree/main/skills/review") == {
        "owner": "acme", "repo": "kit", "path": "skills/review", "ref": "main"}


@pytest.mark.parametrize("text, reason", [
    ("https://gitlab.com/acme/kit", "GitHub"),
    ("github:acme/kit/../secret", ".."),
    ("github:acme/kit//etc", "path"),
    ("github:ac me/kit", "owner"),
    ("github:acme", "owner/repo"),
])
def test_a_non_github_url_or_a_dotdot_path_is_refused_with_the_reason(text, reason):
    with pytest.raises(S.Refused, match=reason):
        S.parse(text)


def test_resolve_reads_the_sha_from_ls_remote(monkeypatch):
    calls = []

    def fake(argv, cwd=None, timeout=60):
        calls.append(argv)
        return f"{SHA}\trefs/heads/main\n"
    monkeypatch.setattr(S, "_git", fake)
    src = S.parse("github:acme/kit@main")
    assert S.resolve(src) == SHA
    assert calls == [["git", "ls-remote", "https://github.com/acme/kit", "main"]]
    calls.clear()
    assert S.resolve({**src, "ref": "b" * 40}) == "b" * 40 and calls == []
    monkeypatch.setattr(S, "_git", lambda argv, cwd=None, timeout=60: "")
    with pytest.raises(S.Refused, match="no such ref"):
        S.resolve(src)


def _tree(root):
    (root / "skills" / "review").mkdir(parents=True)
    (root / "skills" / "review" / "SKILL.md").write_text("---\nname: review\n---\n")
    (root / "skills" / "review" / "check.py").write_text("print(1)\n")
    run = root / "skills" / "review" / "run"
    run.write_text("#!/bin/sh\n")
    (root / "hooks").mkdir()
    (root / "hooks" / "hooks.json").write_text("{}")
    (root / ".claude-plugin").mkdir()
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "kit", "hooks": {}}))
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {}}))
    (root / "README.md").write_text("hi")
    return root


def _fake_git(tree):
    def fake(argv, cwd=None, timeout=60):
        if argv[:2] == ["git", "checkout"]:
            shutil.copytree(tree, cwd, dirs_exist_ok=True)
        return ""
    return fake


def test_inventory_flags_scripts_hooks_and_mcp_by_kind(tmp_path, monkeypatch):
    tree = _tree(tmp_path / "up")
    monkeypatch.setattr(S, "_git", _fake_git(tree))
    C.STATE_DIR = tmp_path / "state"
    C.STATE_DIR.mkdir()
    root = S.fetch(S.parse("github:acme/kit"), SHA)
    assert root.parent == C.STATE_DIR
    inv = S.inventory(root, "")
    assert inv["kind"] == "plugin"
    assert inv["skills"] == ["review"]
    assert "README.md" in inv["files"]
    assert inv["flags"] == [
        {"kind": "script", "file": "skills/review/check.py"},
        {"kind": "script", "file": "skills/review/run"},
        {"kind": "hook", "file": ".claude-plugin/plugin.json"},
        {"kind": "hook", "file": "hooks/hooks.json"},
        {"kind": "mcp", "file": ".mcp.json"},
    ]


def test_a_missing_path_is_refused(tmp_path):
    with pytest.raises(S.Refused, match="no such path at"):
        S.inventory(tmp_path, "nope")


def test_a_link_out_or_a_credential_file_is_refused(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "x").write_text("x")
    tree = tmp_path / "tree"
    tree.mkdir()
    os.symlink(out / "x", tree / "link")
    with pytest.raises(S.Refused, match="link"):
        S.inventory(tree, "")
    os.unlink(tree / "link")
    (tree / "auth.json").write_text("{}")
    with pytest.raises(S.Refused, match="credential"):
        S.inventory(tree, "")
