import json
from pathlib import Path
import pytest

from pl import brain_tools


def test_pl_pr_refuses_a_non_github_url():
    with pytest.raises(ValueError):
        brain_tools.pl_pr("https://gitlab.com/your-org/your-repo/merge_requests/1")
    with pytest.raises(ValueError):
        brain_tools.pl_pr("not a url")
    with pytest.raises(ValueError):
        brain_tools.pl_pr("https://github.com/your-org/your-repo/issues/1")


def test_is_feature_deployed_names_the_pr_and_the_deploy_run_with_links(monkeypatch):
    """AC-3: Given feature Y's card with a merged PR and a deploy run,
    when the owner asks 'Is feature Y deployed?', then the answer names
    the PR and the deploy run with links."""
    calls = []

    def fake_gh(args):
        calls.append(list(args))
        if args[0] == "pr" and args[1] == "view":
            return json.dumps({
                "title": "Slice 3: the code host's reads",
                "state": "MERGED",
                "mergedAt": "2026-10-23T12:00:00Z",
                "mergeCommit": {"oid": "abc1234"},
                "url": "https://github.com/your-org/your-repo/pull/123",
            })
        if args[0] == "run" and args[1] == "list":
            return json.dumps([
                {
                    "name": "deploy",
                    "status": "completed",
                    "conclusion": "success",
                    "url": "https://github.com/your-org/your-repo/actions/runs/456",
                    "createdAt": "2026-10-23T12:05:00Z",
                }
            ])
        return ""

    monkeypatch.setattr(brain_tools, "_gh", fake_gh)

    card = {
        "id": "pl#100",
        "title": "Feature Y",
        "metadata": {
            "pr_urls": ["https://github.com/your-org/your-repo/pull/123"],
        },
    }

    result = brain_tools.is_feature_deployed(card)
    answer = result["answer"] if isinstance(result, dict) else str(result)
    assert "https://github.com/your-org/your-repo/pull/123" in answer
    assert "Slice 3: the code host's reads" in answer or "PR" in answer
    assert "https://github.com/your-org/your-repo/actions/runs/456" in answer
    assert "deploy" in answer

    # Verify calls to gh
    assert len(calls) == 2
    assert calls[0] == ["pr", "view", "123", "--repo", "your-org/your-repo", "--json", "title,state,mergedAt,mergeCommit,url"]
    assert calls[1] == ["run", "list", "--repo", "your-org/your-repo", "--commit", "abc1234", "--json", "name,status,conclusion,url,createdAt"]

    # Verify brain.md instructions exist
    guide_path = Path(__file__).resolve().parent.parent / "src" / "pl" / "brain.md"
    assert guide_path.is_file(), "src/pl/brain.md must exist"
    guide_text = guide_path.read_text()
    assert "pr_urls" in guide_text
    assert "merge commit" in guide_text
