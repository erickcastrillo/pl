import json
import subprocess

SOURCES: list[dict] = []


def _run(argv: list[str]) -> str | None:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def log_source(source: dict) -> None:
    SOURCES.append(source)


def get_logged_sources() -> list[dict]:
    return list(SOURCES)


def clear_logged_sources() -> None:
    SOURCES.clear()


def cite_decision(date: str, link: str) -> str:
    return f"you decided this on {date} ({link})"


def pl_memory_search(query: str, sources: list[dict] | None = None) -> list[dict]:
    out = _run(["pl", "memory", "search", query, "--json"])
    if not out:
        return []
    try:
        data = json.loads(out)
    except (ValueError, TypeError):
        return []
    items = data.get("results", data) if isinstance(data, dict) else data
    if not isinstance(items, list):
        items = [items] if isinstance(items, dict) else []

    results = []
    for item in items:
        if not isinstance(item, dict):
            continue
        results.append(item)
        src = {
            "kind": "memory",
            "date": item.get("date") or item.get("created_at"),
            "link": item.get("link") or item.get("url"),
        }
        if "text" in item:
            src["text"] = item["text"]
        elif "decision" in item:
            src["decision"] = item["decision"]
        log_source(src)
        if sources is not None:
            sources.append(src)
    return results
