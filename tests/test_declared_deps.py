"""Every third-party package pl imports must be declared in pyproject.toml."""

import ast
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULE_TO_DIST = {"yaml": "pyyaml", "markdown_it": "markdown-it-py"}


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirements(pyproject: Path) -> list[str]:
    return tomllib.loads(pyproject.read_text())["project"]["dependencies"]


def declared(pyproject: Path) -> set[str]:
    return {_norm(re.split(r"[\[<>=!~; ]", req, maxsplit=1)[0]) for req in _requirements(pyproject)}


def imported(src: Path, root: Path) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for path in sorted(src.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                modules = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules = [node.module.split(".")[0]]
            else:
                continue
            for module in modules:
                if module in sys.stdlib_module_names or module in ("__future__", "pl"):
                    continue
                found.setdefault(module, set()).add(path.relative_to(root).as_posix())
    return found


def undeclared(src: Path, pyproject: Path, root: Path) -> list[str]:
    have = declared(pyproject)
    return sorted(
        f"{module} imported by {', '.join(sorted(files))}; not in pyproject.toml [project].dependencies"
        for module, files in imported(src, root).items()
        if _norm(MODULE_TO_DIST.get(module, module)) not in have
    )


def test_every_third_party_import_is_declared():
    problems = undeclared(ROOT / "src" / "pl", ROOT / "pyproject.toml", ROOT)
    assert problems == [], "\n".join(problems)


def test_mcp_capped_below_2_and_httpx_declared():
    reqs = _requirements(ROOT / "pyproject.toml")
    assert any(r.startswith("mcp") and "<2" in r for r in reqs)
    httpx = [r for r in reqs if _norm(re.split(r"[\[<>=!~; ]", r, maxsplit=1)[0]) == "httpx"]
    assert httpx
    match = re.search(r">=\s*([\d.]+)", httpx[0])
    assert match and tuple(map(int, match.group(1).split("."))) <= (0, 28, 1)


def test_finds_undeclared_import_in_copy(tmp_path):
    (tmp_path / "src" / "pl").mkdir(parents=True)
    (tmp_path / "src" / "pl" / "a.py").write_text("def f():\n    import requests\n")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\ndependencies = ["httpx"]\n')
    problems = undeclared(tmp_path / "src", tmp_path / "pyproject.toml", tmp_path)
    assert len(problems) == 1
    assert "requests" in problems[0] and "src/pl/a.py" in problems[0]


def test_dropping_httpx_is_reported(tmp_path):
    lines = (ROOT / "pyproject.toml").read_text().splitlines()
    (tmp_path / "pyproject.toml").write_text("\n".join(l for l in lines if '"httpx' not in l.split("=")[0] or l.strip().startswith("#")) + "\n")
    problems = undeclared(ROOT / "src" / "pl", tmp_path / "pyproject.toml", ROOT)
    assert any("httpx" in p and "src/pl/connectors_oauth.py" in p for p in problems)


def test_stdlib_relative_and_pl_imports_ignored(tmp_path):
    (tmp_path / "src" / "pl").mkdir(parents=True)
    (tmp_path / "src" / "pl" / "a.py").write_text(
        "from __future__ import annotations\nimport os\nfrom . import x\nfrom pl import config\n"
    )
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\ndependencies = []\n')
    assert undeclared(tmp_path / "src", tmp_path / "pyproject.toml", tmp_path) == []
