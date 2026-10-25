import json
from pathlib import Path

from pl import brain_tools


def test_a_related_question_cites_the_saved_decision_with_date_and_link(monkeypatch):
    """AC-10: Given an earlier saved decision, when a related question is asked,
    then memory search logs it as a source of kind 'memory' with date and link,
    and brain.md directs citing 'you decided this on <date>' with the link."""
    saved_decision = {
        "text": "keep replies under 120 words",
        "date": "2026-10-20",
        "link": "https://github.com/your-org/your-repo/issues/123#issuecomment-456",
    }
    calls = []

    def fake_run(argv):
        calls.append(argv)
        if argv == ["pl", "memory", "search", "reply length", "--json"]:
            return json.dumps([saved_decision])
        return "[]"

    monkeypatch.setattr(brain_tools, "_run", fake_run)
    brain_tools.clear_logged_sources()

    results = brain_tools.pl_memory_search("reply length")
    assert len(results) == 1
    assert calls == [["pl", "memory", "search", "reply length", "--json"]]

    sources = brain_tools.get_logged_sources()
    assert len(sources) == 1
    src = sources[0]
    assert src["kind"] == "memory"
    assert src["date"] == "2026-10-20"
    assert src["link"] == "https://github.com/your-org/your-repo/issues/123#issuecomment-456"

    # Citation formatting matches the required cite phrase: "you decided this on <date>" with the link
    citation = brain_tools.cite_decision(src["date"], src["link"])
    assert "you decided this on 2026-10-20" in citation
    assert "https://github.com/your-org/your-repo/issues/123#issuecomment-456" in citation

    # brain.md instructs memory search before answering and citing the decision
    guide_path = Path(__file__).resolve().parent.parent / "src" / "pl" / "brain.md"
    assert guide_path.is_file(), "src/pl/brain.md must exist"
    guide_text = guide_path.read_text()
    assert "search memory before answering" in guide_text.lower()
    assert 'you decided this on <date>' in guide_text
