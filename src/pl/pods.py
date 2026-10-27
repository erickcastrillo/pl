"""Pods: a pipeline's rules (pod.md, kept as numbered versions with rule add and rollback), knowledge, skills, plugins and layer budgets."""
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

from pl import config as C
from pl.util import ask, now_iso, parse_iso, short_id

BUILTIN_DIR = Path(__file__).parent / "pods_builtin"
BUDGETS = {"knowledge": 2000, "pod": 1500, "client": 1500}   # tokens; the role and card layers are uncut
ENGINEERING_SKILLS = ["pl-spec", "pl-design", "pl-plan", "pl-run", "pl-review"]
SALES_SKILLS = ["pl-sales-proposal", "pl-sales-follow-up"]
BUILTIN_SKILLS = {"engineering": ENGINEERING_SKILLS, "marketing": ["pl-stage"], "sales": [*SALES_SKILLS, "pl-stage"]}
BUILTIN_ROLES = {"sales": ["contacts", "email"]}   # connector roles a built-in pod works with
OWN_SKILLS_ONLY = ("marketing",)   # built-in pods that limit a launch to their own skills
PIPELINE_POD_ONLY = ("sales",)     # built-in pods only an explicit [pipeline] pod picks, never the profile's name


def _resolve(path):
    p = Path(path).expanduser()
    return p if p.is_absolute() or C.CONFIG_DIR is None else C.CONFIG_DIR / p


def _builtin(name="engineering"):
    own = pod_file(name) if C.CONFIG_DIR else None
    return {"name": name, "file": own if own and own.is_file() else BUILTIN_DIR / name / "pod.md", "knowledge": [],
            "skills": list(BUILTIN_SKILLS[name]), "roles": list(BUILTIN_ROLES.get(name, [])), "plugins": [],
            "budgets": dict(BUDGETS)}


def skill_dir(name):
    """The folder of a skill a built-in pod ships in its own folder, else None."""
    return next((d for d in BUILTIN_DIR.glob(f"*/skills/{name}") if (d / "SKILL.md").is_file()), None)


def _configured(name):
    v = C.PODS[name]
    return {"name": name, "file": _resolve(v["file"]) if v.get("file") else None,
            "knowledge": [_resolve(k) for k in v.get("knowledge", [])], "skills": list(v.get("skills", [])),
            "roles": list(v.get("roles", [])),
            "plugins": [{**p, "hooks": p.get("hooks", True)} for p in v.get("plugins", [])],
            "budgets": {**BUDGETS, **v.get("budgets", {})}}


def find(name):
    if name in C.PODS and name in OWN_SKILLS_ONLY + PIPELINE_POD_ONLY:
        table = _configured(name)
        return {**_builtin(name), **{k: table[k] for k in C.PODS[name] if k in table}}
    if name in C.PODS:
        return _configured(name)
    return _builtin(name) if name in BUILTIN_SKILLS else None


def current():
    """The pipeline's pod: [pipeline] pod, else the profile's name, else the built-in engineering pod."""
    pod = find(C.POD or C.PROFILE_NAME)
    if pod is None and C.POD:
        raise SystemExit(f"pl: [pipeline] pod names {C.POD!r}, which is not in [pods]")
    return pod or find("engineering")


def launch_pod():
    """The pod whose skills a launch is limited to: only one the profile configures with a `skills` key, so a profile without pods, or a pod table that sets only file or knowledge, keeps the whole library."""
    name = next((n for n in (C.POD or C.PROFILE_NAME, "engineering") if n in C.PODS), None)
    if name:
        return find(name) if "skills" in C.PODS[name] or name in OWN_SKILLS_ONLY + PIPELINE_POD_ONLY else None
    own = C.POD or C.PROFILE_NAME
    return find(own) if own in OWN_SKILLS_ONLY or C.POD in PIPELINE_POD_ONLY else None


def names():
    return sorted({*BUILTIN_SKILLS, *C.PODS})


def rules(pod):
    if not pod["file"] or not pod["file"].is_file():
        return []
    return [ln.strip()[2:].strip() for ln in pod["file"].read_text().splitlines() if ln.strip()[:2] in ("- ", "* ")]


def _version(name):
    try:
        return len(history(name)) or 1
    except (TypeError, OSError):
        return 1


def describe(pod):
    return {"name": pod["name"], "file": str(pod["file"]) if pod["file"] and pod["file"].is_file() else None,
            "rules": rules(pod), "roles": pod["roles"],
            "knowledge": [str(k) for k in pod["knowledge"]], "skills": pod["skills"], "plugins": pod["plugins"],
            "budgets": pod["budgets"], "version": _version(pod["name"])}


ESCALATION_ALERTS = ("stage_failed:", "api_error:")


def _edit_count(pod_name, cutoff):
    try:
        lines = _edits_path().read_text().splitlines()
    except OSError:
        return 0
    n = 0
    for ln in lines:
        try:
            rec = json.loads(ln)
            ts = parse_iso(rec["ts"]) if isinstance(rec.get("ts"), str) else rec.get("ts", 0)
        except (ValueError, KeyError, TypeError):
            continue
        n += rec.get("pod") == pod_name and ts >= cutoff
    return n


def metrics(pod, days=30):
    """Approval rate, edits per approved draft and escalations of the pod over the last days; the profile's events
    belong to the pipeline's pod, so any other pod reads as empty."""
    from datetime import datetime, timedelta, timezone

    from pl import alerts, events
    window = {"window_days": days, "approval_rate": None, "edits_per_draft": None, "escalations": 0}
    if pod != current()["name"]:
        return window
    since = datetime.now(timezone.utc) - timedelta(days=days)
    seen = [e for e in events._read() if e["_t"] >= since]
    kinds = [e["kind"] for e in seen]
    approved = kinds.count("approved") + kinds.count("spec_approved")
    decided = approved + kinds.count("rejected") + kinds.count("spec_rejected")
    window["approval_rate"] = approved / decided if decided else None
    window["edits_per_draft"] = _edit_count(pod, since.timestamp()) / approved if approved else None
    manual = sum(e["kind"] == "moved" and e.get("to") == "Manual" for e in seen)
    failures = sum(a["key"].startswith(ESCALATION_ALERTS) and (a.get("first_seen") or 0) >= since.timestamp()
                   for a in alerts.listing(all=True))
    window["escalations"] = failures + manual
    return window


def _cmd_list_show(a):
    """pl pods list [--json] | show NAME [--json]."""
    if a.pods_cmd == "list":
        print(json.dumps(names()) if a.json else "\n".join(names()))
        return 0
    pod = find(a.name)
    if pod is None:
        print(f"pl: no pod {a.name!r}; pods: {', '.join(names())}", file=sys.stderr)
        raise SystemExit(2)
    d = describe(pod)
    if a.json:
        d["metrics"] = metrics(pod["name"])
        print(json.dumps(d, indent=2))
        return 0
    print(f"{d['name']}  (version {d['version']})  {d['file'] or 'no pod.md'}")
    print("rules:\n" + "\n".join(f"  - {r}" for r in d["rules"]))
    for k in ("knowledge", "skills", "roles", "plugins"):
        print(f"{k}: " + (", ".join(x["name"] if isinstance(x, dict) else x for x in d[k]) or "none"))
    print("budgets: " + ", ".join(f"{k} {v}" for k, v in d["budgets"].items()))
    return 0


RULES_HEADING = "## Rules"


def pod_file(pod: str) -> Path:
    cfg = (getattr(C, "PODS", {}) or {}).get(pod) or {}
    if cfg.get("file"):
        p = Path(cfg["file"]).expanduser()
        return p if p.is_absolute() else C.CONFIG_DIR / p
    return C.CONFIG_DIR / "pods" / pod / "pod.md"


def _history_dir(pod: str) -> Path:
    return C.CONFIG_DIR / "pods" / ".history" / pod


def _shipped(pod: str) -> bytes | None:
    f = BUILTIN_DIR / pod / "pod.md"
    return f.read_bytes() if pod in BUILTIN_SKILLS and f.is_file() else None


def _write_private(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def history(pod: str) -> list[dict]:
    try:
        return json.loads((_history_dir(pod) / "versions.json").read_text())
    except (OSError, ValueError):
        return []


pod_history = lambda pod: history(pod)   # noqa: E731  the name the draft-learning code and its tests use


def _record(pod: str, data: bytes, note: str, author: str) -> int:
    versions = history(pod)
    n = len(versions) + 1
    d = _history_dir(pod)
    _write_private(d / f"{n}.md", data)
    versions.append({"n": n, "ts": now_iso(), "note": note, "author": author,
                     "sha256": hashlib.sha256(data).hexdigest()})
    _write_private(d / "versions.json", json.dumps(versions, indent=1).encode())
    return n


def save_version(pod: str, text: str | bytes, note: str, author: str) -> int:
    """Write pod.md as a new version; a copy that was changed outside pl is kept as a version first."""
    data = text.encode() if isinstance(text, str) else text
    p = pod_file(pod)
    versions = history(pod)
    if not p.is_file() and not versions and (shipped := _shipped(pod)) is not None:
        _record(pod, shipped, "built-in pod.md", author)
        versions = history(pod)
    if p.is_file():
        current = p.read_bytes()
        if not versions:
            _record(pod, current, "existing pod.md", author)
        elif hashlib.sha256(current).hexdigest() != versions[-1]["sha256"]:
            _record(pod, current, "edited outside pl", author)
    n = _record(pod, data, note, author)
    _write_private(p, data)
    return n


def add_rule(text: str, rule: str) -> str:
    text = text if text.endswith("\n") or not text else text + "\n"
    if RULES_HEADING not in text.splitlines():
        text += ("\n" if text else "") + RULES_HEADING + "\n"
    lines = text.splitlines()
    i = lines.index(RULES_HEADING) + 1
    while i < len(lines) and not lines[i].startswith("## "):
        i += 1
    while i > 0 and not lines[i - 1].strip():
        i -= 1
    lines.insert(i, f"- {rule}")
    return "\n".join(lines) + "\n"


def _confirm(question: str, yes: bool):
    if yes:
        return
    try:
        ok = ask("pods", f"{question} [y/N]: ")
    except EOFError:
        ok = ""
    if ok.strip().lower() not in ("y", "yes"):
        print("nothing changed")
        raise SystemExit(0)


def _author() -> str:
    return os.environ.get("USER") or "unknown"


def _cmd_rule(a) -> int:
    _confirm(f"Add this rule to {a.pod}: {a.text}?", a.yes)
    p = pod_file(a.pod)
    shipped = _shipped(a.pod)
    current = p.read_text() if p.is_file() else shipped.decode() if shipped else ""
    n = save_version(a.pod, add_rule(current, a.text), a.note or f"rule: {a.text}", _author())
    print(f"added the rule to {a.pod} (version {n})")
    return 0


def _cmd_rollback(a) -> int:
    d = _history_dir(a.pod)
    if a.to not in {v["n"] for v in history(a.pod)}:
        print(f"pl pods: {a.pod} has no version {a.to}", file=sys.stderr)
        raise SystemExit(2)
    _confirm(f"Roll {a.pod} back to version {a.to}?", a.yes)
    n = save_version(a.pod, (d / f"{a.to}.md").read_bytes(), f"rollback to {a.to}", _author())
    print(f"{a.pod} is back to version {a.to}, saved as version {n}")
    return 0


def _cmd_history(a) -> int:
    versions = history(a.pod)
    if a.json:
        print(json.dumps(versions, indent=1))
    for v in ([] if a.json else versions):
        print(f"{v['n']:>3}  {v['ts']}  {v['author']}  {v['note']}")
    return 0


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
    return getattr(C, "POD", None) or getattr(C, "PROFILE_NAME", None) or "engineering"


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


def _read_pod(pod: str) -> str:
    p = pod_file(pod)
    return p.read_text() if p.is_file() else ""


def learn(card, section, draft, approved, pod=None):
    """Compare draft and approved text, record removed sentences, and suggest rules."""
    from pl import alerts, trackers
    if trackers.dry_run():
        return
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
    save_version(s["pod"], add_rule(_read_pod(s["pod"]), s["rule"]), f"from {s.get('seen', 3)} edited drafts", _author())
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


def cmd_pods(a) -> int:
    """pl pods list [--json] | show NAME [--json] | rule add POD "TEXT" [--yes] [--note N] | rule accept ID | rule dismiss ID
    | rollback POD --to N [--yes] | history POD [--json]."""
    if a.pods_cmd in ("list", "show"):
        return _cmd_list_show(a)
    if a.pods_cmd == "rule" and getattr(a, "rule_cmd", None) == "accept":
        return cmd_pods_rule_accept(a) or 0
    if a.pods_cmd == "rule" and getattr(a, "rule_cmd", None) == "dismiss":
        return cmd_pods_rule_dismiss(a) or 0
    return {"rule": _cmd_rule, "rollback": _cmd_rollback, "history": _cmd_history}[a.pods_cmd](a)
