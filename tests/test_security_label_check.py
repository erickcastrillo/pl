"""The security label check: PRs touching security-sensitive paths must carry pl:security-reviewed."""
from pathlib import Path
import sys

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / ".github" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import security_label_check  # noqa: E402


def pr(**kw):
    base = {
        "draft": False,
        "labels": [{"name": "pl:security-reviewed"}],
        "base": {"ref": "main"},
        "head": {"sha": "abc123"},
    }
    base.update(kw)
    return base


def f(name, **kw):
    return {"filename": name, "status": "modified", **kw}


COMMITTED = "2026-10-09T10:00:00Z"
LABELED = "2026-10-09T11:00:00Z"


def check(p=None, files=None, action="labeled", labeled_at=LABELED, committed_at=COMMITTED):
    return security_label_check.check(
        p or pr(),
        files or [f("src/pl/connectors/github.py")],
        action=action,
        labeled_at=labeled_at,
        committed_at=committed_at,
    )


def test_a_pr_touching_connectors_without_the_label_fails():
    files = [f("src/pl/connectors/github.py")]
    p = pr(labels=[])
    reasons = check(p=p, files=files)
    assert reasons and any("security" in r.lower() for r in reasons)


def test_a_label_older_than_the_last_commit_fails():
    reasons = check(labeled_at="2026-10-09T09:00:00Z", committed_at="2026-10-09T10:00:00Z")
    assert reasons and any("before the last commit" in r for r in reasons)


def test_an_unrelated_pr_passes():
    files = [f("src/pl/watch.py")]
    p = pr(labels=[])
    assert check(p=p, files=files) == []


def test_tag_check_fails_an_unverified_tag():
    assert not security_label_check.check_tag({"verification": {"verified": False}})
    assert not security_label_check.check_tag({})


def test_tag_check_passes_a_verified_tag():
    assert security_label_check.check_tag({"verification": {"verified": True}})


def test_main_tag_check_fails_unverified(monkeypatch, capsys):
    monkeypatch.setattr(security_label_check, "_gh", lambda args: '{"verification": {"verified": false}}')
    assert security_label_check.main(["--tag", "owner/pl", "v1.0.0"]) == 1
    assert "not signed and verified" in capsys.readouterr().err


def test_main_tag_check_passes_verified(monkeypatch, capsys):
    monkeypatch.setattr(security_label_check, "_gh", lambda args: '{"verification": {"verified": true}}')
    assert security_label_check.main(["--tag", "owner/pl", "v1.0.0"]) == 0
    assert "verified" in capsys.readouterr().out
