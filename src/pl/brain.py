"""Central brain: ask questions and save decisions."""
from typing import Any


def ask(text: str, conv: str | None = None) -> dict[str, Any]:
    return {"text": f"Answer for: {text}", "sources": []}


def remember(text: str, conv: str | None = None) -> dict[str, Any]:
    return {"text": f"Saved: {text}", "sources": []}


def chat_reply(answer: Any) -> str:
    from pl.chat import safe_text

    text = (
        answer.get("text") or answer.get("answer") or ""
        if isinstance(answer, dict)
        else getattr(answer, "text", getattr(answer, "answer", str(answer)))
    )
    sources = (answer.get("sources") if isinstance(answer, dict) else getattr(answer, "sources", [])) or []
    cleaned = safe_text(text)
    links = []
    for s in sources[:5]:
        t = s.get("title", "") if isinstance(s, dict) else getattr(s, "title", "")
        u = s.get("url", "") if isinstance(s, dict) else getattr(s, "url", "")
        if t and u:
            links.append(f"{t}: {u}")
        elif u or t:
            links.append(str(u or t))
    return (cleaned + "\n\n" + "\n".join(links)).strip() if links else cleaned
