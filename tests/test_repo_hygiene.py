"""Tests for repository security hygiene (SECURITY.md, threat model, dependabot, ci audit)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_security_md_threat_model_and_dependency_scanning_exist():
    sec_md = ROOT / "SECURITY.md"
    assert sec_md.is_file(), "SECURITY.md must exist"
    sec_text = sec_md.read_text()
    assert "vulnerability" in sec_text.lower()
    assert "scope" in sec_text.lower()
    assert "response" in sec_text.lower()

    tm_md = ROOT / "docs" / "threat-model.md"
    assert tm_md.is_file(), "docs/threat-model.md must exist"
    tm_text = tm_md.read_text()
    assert "threat" in tm_text.lower()

    dependabot = ROOT / ".github" / "dependabot.yml"
    assert dependabot.is_file(), ".github/dependabot.yml must exist"
    dep_text = dependabot.read_text()
    assert "pip" in dep_text
    assert "github-actions" in dep_text

    ci_yml = ROOT / ".github" / "workflows" / "ci.yml"
    assert ci_yml.is_file(), "ci.yml must exist"
    ci_text = ci_yml.read_text()
    assert "audit:" in ci_text
    assert "pip-audit" in ci_text
    assert "uv export --frozen" in ci_text
