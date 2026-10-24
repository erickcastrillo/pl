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


def draft_path(card, section) -> Path:
    """The local snapshot file of a draft when the card reached a gate column."""
    name = f"{str(card).replace('/', '_')}-{section}.md"
    p = C.STATE_DIR / "drafts" / name
    if p.is_file():
        return p
    short = f"{short_id(str(card)).replace('/', '_')}-{section}.md"
    p_short = C.STATE_DIR / "drafts" / short
    return p_short if p_short.is_file() else p


def save_draft(card, section, text: str) -> Path:
    """Snapshot a draft section (0600) when reaching a gate column."""
    d = C.STATE_DIR / "drafts"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    p = d / f"{str(card).replace('/', '_')}-{section}.md"
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text or "")
    return p


def split_sentences(text: str) -> list[str]:
    """Split text into sentences or list items."""
    if not text:
        return []
    chunks = re.split(r"(?<=[.!?])\s+|\n+", text)
    out = []
    for chunk in chunks:
        s = chunk.strip()
        s = MARKDOWN_LEAD_RE.sub("", s).strip()
        if s:
            out.append(s)
    return out


def normalize_sentence(sentence: str) -> str:
    """Normalize a sentence: lowercase, whitespace collapsed, numbers replaced with '#'."""
    s = sentence.lower().strip()
    s = NORM_NUM_RE.sub("#", s)
    s = NORM_SPACE_RE.sub(" ", s)
    return s.strip()


def sentence_hash(norm: str) -> str:
    """Hash of a normalized sentence."""
    return hashlib.sha256(norm.encode()).hexdigest()[:16]


def pod_of(card=None, explicit_pod=None) -> str:
    """The pod name for a card or current pipeline."""
    if explicit_pod:
        return explicit_pod
    if isinstance(card, dict):
        m = card.get("metadata") or {}
        if m.get("pod"):
            return m["pod"]
    pipe = getattr(C, "PIPELINE", {}) or {}
    if pipe.get("pod"):
        return pipe["pod"]
    return getattr(C, "PROFILE_NAME", None) or "engineering"


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
    C.STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    p = _suggestions_path()
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(suggs, f, indent=1)


def _record_edit(pod: str, s_hash: str, sentence: str, card, ts: str):
    C.STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    p = _edits_path()
    line = json.dumps({"pod": pod, "sentence_hash": s_hash, "sentence": sentence, "card": str(card), "ts": ts})
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(line + "\n")


def pod_file(pod: str) -> Path:
    """The path to pod.md for the given pod."""
    pod_cfg = (getattr(C, "PODS", {}) or {}).get(pod) or {}
    if pod_cfg.get("file"):
        p = Path(pod_cfg["file"]).expanduser()
        return p if p.is_absolute() else (C.CONFIG_DIR or Path()) / p
    base = (C.CONFIG_DIR or Path()) / "pods"
    p = base / pod / "pod.md"
    if p.is_file():
        return p
    p2 = base / f"{pod}.md"
    if p2.is_file():
        return p2
    return p


def _history_dir(pod: str) -> Path:
    d = (C.CONFIG_DIR or Path()) / "pods" / ".history" / pod
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def pod_history(pod: str) -> list[dict]:
    """Numbered versions history for the pod."""
    hdir = _history_dir(pod)
    entries = []
    for meta_file in sorted(hdir.glob("*.json")):
        try:
            data = json.loads(meta_file.read_text())
            if isinstance(data, dict):
                entries.append(data)
        except (OSError, ValueError):
            continue
    entries.sort(key=lambda x: x.get("version", 0))
    return entries


def rule_add(pod: str, rule: str, note: str = "") -> int:
    """Add a rule to pod.md as a new version with history."""
    p = pod_file(pod)
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    content = p.read_text() if p.is_file() else f"# {pod.title()} Pod Rules\n"
    if not content.endswith("\n"):
        content += "\n"
    content += f"- {rule}\n"

    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(content)

    hdir = _history_dir(pod)
    existing = list(hdir.glob("*.md"))
    ver = len(existing) + 1
    ver_md = hdir / f"{ver}.md"
    ver_json = hdir / f"{ver}.json"

    fd = os.open(ver_md, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(content)

    meta = {"version": ver, "note": note, "ts": now_iso(), "rule": rule}
    fd = os.open(ver_json, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(meta, f, indent=1)

    return ver


def learn(card, section, draft, approved, pod=None):
    """Compare draft and approved text, record removed sentences, and suggest rules."""
    from pl import alerts

    pod_name = pod_of(card, explicit_pod=pod)
    draft_sentences = split_sentences(draft)
    approved_sentences = split_sentences(approved)
    approved_norm = {normalize_sentence(s) for s in approved_sentences}

    seen_norm = set()
    ts = now_iso()
    now = time.time()
    window_sec = RULE_WINDOW_DAYS * 86400

    for s in draft_sentences:
        norm = normalize_sentence(s)
        if norm and norm not in approved_norm and norm not in seen_norm:
            seen_norm.add(norm)
            h = sentence_hash(norm)
            _record_edit(pod_name, h, s, card, ts)

            # Check if this sentence was removed from 3 approved drafts in one pod within 30 days
            p_edits = _edits_path()
            if not p_edits.is_file():
                continue
            cards_seen = set()
            for line in p_edits.read_text().splitlines():
                try:
                    rec = json.loads(line)
                    if rec.get("pod") == pod_name and rec.get("sentence_hash") == h:
                        rec_ts = parse_iso(rec.get("ts", "")) if isinstance(rec.get("ts"), str) else rec.get("ts", 0)
                        if now - rec_ts <= window_sec:
                            cards_seen.add(rec.get("card"))
                except (ValueError, TypeError):
                    continue

            if len(cards_seen) >= RULE_MIN_SEEN:
                suggs = _load_suggestions()
                s_id = h[:8]
                existing = suggs.get(s_id)
                if existing:
                    if existing.get("status") in ("dismissed", "accepted"):
                        continue
                    existing["seen"] = len(cards_seen)
                    _save_suggestions(suggs)
                else:
                    rule_text = f"Never write: {s}"
                    suggs[s_id] = {
                        "id": s_id,
                        "pod": pod_name,
                        "rule": rule_text,
                        "seen": len(cards_seen),
                    }
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
    sub = getattr(a, "pods_cmd", None)
    if sub == "rule":
        rc = getattr(a, "rule_cmd", None)
        if rc == "accept":
            return cmd_pods_rule_accept(a)
        if rc == "dismiss":
            return cmd_pods_rule_dismiss(a)
        if rc == "add":
            return cmd_pods_rule_add(a)
    raise SystemExit("pl pods: unknown subcommand (see pl pods --help)")
