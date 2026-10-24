"""Pods: pipeline context, knowledge, rules and learning from draft edits."""
import hashlib
import json
import os
import re
import time
from pathlib import Path

from pl import config as C
from pl.util import now_iso, parse_iso, short_id

RULE_MIN_SEEN = 3
RULE_WINDOW_DAYS = 30
NORM_NUM_RE = re.compile(r"\d+")
NORM_SPACE_RE = re.compile(r"\s+")
MARKDOWN_LEAD_RE = re.compile(r"^([-*•#]+|\d+[.)])\s+")


def _write_file(p: Path, text: str, mode: str = "w") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if "a" in mode else os.O_TRUNC)
    fd = os.open(p, flags, 0o600)
    with os.fdopen(fd, mode) as f:
        f.write(text)
    return p


def draft_path(card, section) -> Path:
    """The local snapshot file of a draft when the card reached a gate column."""
    p = C.STATE_DIR / "drafts" / f"{str(card).replace('/', '_')}-{section}.md"
    if p.is_file():
        return p
    p_short = C.STATE_DIR / "drafts" / f"{short_id(str(card)).replace('/', '_')}-{section}.md"
    return p_short if p_short.is_file() else p


def save_draft(card, section, text: str) -> Path:
    """Snapshot a draft section (0600) when reaching a gate column."""
    return _write_file(C.STATE_DIR / "drafts" / f"{str(card).replace('/', '_')}-{section}.md", text or "")


def split_sentences(text: str) -> list[str]:
    """Split text into sentences or list items."""
    out = []
    for chunk in re.split(r"(?<=[.!?])\s+|\n+", text or ""):
        if s := MARKDOWN_LEAD_RE.sub("", chunk.strip()).strip():
            out.append(s)
    return out


def normalize_sentence(sentence: str) -> str:
    """Normalize a sentence: lowercase, whitespace collapsed, numbers replaced with '#'."""
    return NORM_SPACE_RE.sub(" ", NORM_NUM_RE.sub("#", sentence.lower().strip())).strip()


def sentence_hash(norm: str) -> str:
    return hashlib.sha256(norm.encode()).hexdigest()[:16]


def pod_of(card=None, explicit_pod=None) -> str:
    """The pod name for a card or current pipeline."""
    if explicit_pod:
        return explicit_pod
    if isinstance(card, dict) and (m := card.get("metadata") or {}).get("pod"):
        return m["pod"]
    return (getattr(C, "PIPELINE", {}) or {}).get("pod") or getattr(C, "PROFILE_NAME", None) or "engineering"


def _edits_path() -> Path:
    return C.STATE_DIR / "pod-edits.jsonl"


def _suggestions_path() -> Path:
    return C.STATE_DIR / "pod-suggestions.json"


def _load_suggestions() -> dict:
    p = _suggestions_path()
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else {x["id"]: x for x in data if isinstance(x, dict) and "id" in x}
    except (OSError, ValueError):
        return {}


def _save_suggestions(suggs: dict):
    _write_file(_suggestions_path(), json.dumps(suggs, indent=1))


def pod_file(pod: str) -> Path:
    """The path to pod.md for the given pod."""
    if cfg_file := (getattr(C, "PODS", {}) or {}).get(pod, {}).get("file"):
        p = Path(cfg_file).expanduser()
        return p if p.is_absolute() else (C.CONFIG_DIR or Path()) / p
    base = (C.CONFIG_DIR or Path()) / "pods"
    return base / pod / "pod.md" if (base / pod / "pod.md").is_file() else (
        base / f"{pod}.md" if (base / f"{pod}.md").is_file() else base / pod / "pod.md"
    )


def _history_dir(pod: str) -> Path:
    d = (C.CONFIG_DIR or Path()) / "pods" / ".history" / pod
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def pod_history(pod: str) -> list[dict]:
    """Numbered versions history for the pod."""
    entries = []
    for meta_file in sorted(_history_dir(pod).glob("*.json")):
        try:
            if isinstance(data := json.loads(meta_file.read_text()), dict):
                entries.append(data)
        except (OSError, ValueError):
            continue
    return sorted(entries, key=lambda x: x.get("version", 0))


def rule_add(pod: str, rule: str, note: str = "") -> int:
    """Add a rule to pod.md as a new version with history."""
    p = pod_file(pod)
    content = p.read_text() if p.is_file() else f"# {pod.title()} Pod Rules\n"
    content = (content if content.endswith("\n") else content + "\n") + f"- {rule}\n"
    _write_file(p, content)

    hdir = _history_dir(pod)
    ver = len(list(hdir.glob("*.md"))) + 1
    _write_file(hdir / f"{ver}.md", content)
    _write_file(hdir / f"{ver}.json", json.dumps({"version": ver, "note": note, "ts": now_iso(), "rule": rule}, indent=1))
    return ver


def learn(card, section, draft, approved, pod=None):
    """Compare draft and approved text, record removed sentences, and suggest rules."""
    from pl import alerts
    pod_name = pod_of(card, explicit_pod=pod)
    approved_norm = {normalize_sentence(s) for s in split_sentences(approved)}
    seen_norm, ts, now = set(), now_iso(), time.time()

    for s in split_sentences(draft):
        norm = normalize_sentence(s)
        if not norm or norm in approved_norm or norm in seen_norm:
            continue
        seen_norm.add(norm)
        h = sentence_hash(norm)
        line = json.dumps({"pod": pod_name, "sentence_hash": h, "sentence": s, "card": str(card), "ts": ts})
        _write_file(_edits_path(), line + "\n", mode="a")

        if not _edits_path().is_file():
            continue
        cards_seen = set()
        for ln in _edits_path().read_text().splitlines():
            try:
                rec = json.loads(ln)
                if rec.get("pod") == pod_name and rec.get("sentence_hash") == h:
                    rec_ts = parse_iso(rec.get("ts", "")) if isinstance(rec.get("ts"), str) else rec.get("ts", 0)
                    if now - rec_ts <= RULE_WINDOW_DAYS * 86400:
                        cards_seen.add(rec.get("card"))
            except (ValueError, TypeError):
                continue

        if len(cards_seen) >= RULE_MIN_SEEN:
            suggs, s_id = _load_suggestions(), h[:8]
            if existing := suggs.get(s_id):
                if existing.get("status") in ("dismissed", "accepted"):
                    continue
                existing["seen"] = len(cards_seen)
                _save_suggestions(suggs)
            else:
                rule_text = f"Never write: {s}"
                suggs[s_id] = {"id": s_id, "pod": pod_name, "rule": rule_text, "seen": len(cards_seen)}
                _save_suggestions(suggs)
                print(f"pl suggests a pod rule for {pod_name}: '{rule_text}'; accept with pl pods rule accept {s_id}")
                alerts.open(f"pod_rule:{s_id}", "info", f"Pod rule suggested: {rule_text}", f"pl pods rule accept {s_id}")


def cmd_pods_rule_accept(a):
    from pl import alerts
    suggs = _load_suggestions()
    if a.id not in suggs:
        raise SystemExit(f"pl pods rule: no suggestion {a.id}")
    s = suggs[a.id]
    rule_add(s["pod"], s["rule"], note=f"from {s.get('seen', 3)} edited drafts")
    s["status"] = "accepted"
    _save_suggestions(suggs)
    alerts.resolve(f"pod_rule:{a.id}")
    print(f"accepted rule for {s['pod']}: {s['rule']}")


def cmd_pods_rule_dismiss(a):
    from pl import alerts
    suggs = _load_suggestions()
    if a.id not in suggs:
        raise SystemExit(f"pl pods rule: no suggestion {a.id}")
    s = suggs[a.id]
    s["status"] = "dismissed"
    _save_suggestions(suggs)
    alerts.resolve(f"pod_rule:{a.id}")
    print(f"dismissed suggestion {a.id}")


def cmd_pods_rule_add(a):
    ver = rule_add(a.pod, a.rule, note="added by owner")
    print(f"added rule to {a.pod} (version {ver}): {a.rule}")


def cmd_pods(a):
    if getattr(a, "pods_cmd", None) == "rule":
        rc = getattr(a, "rule_cmd", None)
        if rc == "accept":
            return cmd_pods_rule_accept(a)
        if rc == "dismiss":
            return cmd_pods_rule_dismiss(a)
        if rc == "add":
            return cmd_pods_rule_add(a)
    raise SystemExit("pl pods: unknown subcommand (see pl pods --help)")
