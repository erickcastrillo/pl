"""The pl subcommands (every cmd_* except dispatch and watch)."""
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from pl import config as C
from pl.accounts import exhausted_profiles, next_profile, profile_state, unpark_machine
from pl.agents import NEW_WINDOW_SCRIPT, registry, worker_status, worker_view
from pl import events, ideas, trackers
from pl.board import card, cards, check_size, col_id, col_name, find_card, fresh_next, lists, render, sections, share, update
from pl.dispatch import MAX_ATTEMPTS, approved_label, busy_agents, paused
from pl.product import (linked_product_ids, load_seen, product_card, product_cards_mine, product_col, product_lists,
                        intake_configured, pull_new, pull_one, pull_reason_to_skip)
from pl.util import age, notify, now_iso, parse_iso, slug_of, tmux


def cmd_idea(a):
    text = a.text.strip()
    if not text:
        raise SystemExit("pl idea: the idea text is empty")
    direct = not intake_configured()     # no [intake]: the idea goes straight into the pipeline's Inbox
    line = text.splitlines()[0].strip() if direct else text
    title = a.title or (line if len(line) <= 70 else line[:70].rsplit(" ", 1)[0])
    body, docs = [text, ""], []
    for d in a.doc:
        p = Path(d).expanduser().resolve()
        if not p.is_file():
            raise SystemExit(f"pl idea: no such file {d}")
        docs.append(str(p))
        body.append(f"## Attached: {p.name} ({p})")
        try:
            content = p.read_text()
            body.append("(file over 200 KB, not inlined; read it from the path above)" if len(content) > 200_000
                        else "```\n" + content.rstrip() + "\n```")
        except UnicodeDecodeError:
            body.append("(binary file, not inlined; read it from the path above)")
        body.append("")
    # With an intake board, ideas go to its Triage to be assessed (decision 2026-09-25). Unassigned by default;
    # --start assigns it to you, and `pl pull` then feeds it into the pipeline.
    if not direct and "Triage" not in product_lists():
        raise SystemExit("pl idea: the intake board has no Triage column")
    meta = {"source": "pl idea", **({"submitted_by": C.USER_EMAIL} if C.USER_EMAIL else {}), "submitted_at": now_iso(),
            "submitted_from": C.HOST, "input_docs": docs, **({"preferred_profile": a.account} if a.account else {})}
    desc = "\n".join(["## Where this came from", f"`pl idea`{f' by {C.USER_EMAIL}' if C.USER_EMAIL else ''} on {now_iso()[:10]}.", "", "## The idea"] + body)
    if direct:
        meta = {**meta, "pipeline_mode": "auto", **({"profile": a.account} if a.account else {})}
        desc, title = render({"INPUT": ideas._plain(desc)}), ideas.clean_title(title)
    check_size(desc)
    t = trackers.get("tracker" if direct else "intake")
    item = t.create("Inbox" if direct else "Triage", title=title, description=desc, tags=["idea", "from-pl-idea"] + list(a.repo),
                    metadata=meta, assigned_to=trackers.me(t) if a.start else None)
    if not item.get("id"):
        raise SystemExit(f"pl idea: create failed: {json.dumps(item)[:300]}")
    events.emit("idea_approved", item["id"])
    where = "pipeline board, Inbox" if direct else "intake board, Triage"
    print(f"created {item['id'] if direct else item['id'][:8]}  {title}\n  {where}, {len(docs)} document(s)\n  {t.url(item['id'])}")
    print("  the dispatcher writes its spec on its next pass" if direct
          else "  assigned to you: the pipeline pulls it in within a few minutes" if a.start
          else "  unassigned, waiting to be assessed; assign it to yourself (or rerun with --start) to start the pipeline")



def cmd_list(a):
    bad = exhausted_profiles()
    if bad:
        stp = profile_state()
        print("PROFILES parked (out of usage credits): " + ", ".join(f"{p} until {stp[p]['until'][11:16]} UTC" for p in sorted(bad)) + "   (pl profiles)")
    if paused():
        n = busy_agents(registry())
        print(f"DISPATCHER PAUSED since {paused().get('since', '?')[:16]} UTC: no new agents start   (pl resume)   "
              + (f"draining: {n} agent(s) still working" if n else "DRAINED: safe to shut down"))
    reg = registry()
    share("read")   # the dispatcher's board read, while it is young
    pipeline = cards()
    if not a.product:
        by_col = {}
        for c in pipeline:
            by_col.setdefault(col_name(c["list_id"]), []).append(c)
        print("FEATURE PIPELINE board")
        for col in C.COLUMNS:
            if col == "Done" and not a.all:
                continue
            rows = sorted(by_col.get(col, []), key=lambda c: c.get("updated_at") or "", reverse=True)
            print(f"{col}  ({len(rows)})")
            for c in rows:
                print(f"  {c['id'][:8]}  {c['title'][:56]:<56}  {approved_label(c, col, reg) or worker_view(c, reg):<28} {age(parse_iso(c.get('updated_at') or ''))}")
        print()
    # Intake board: everything assigned to you, linked funnel cards marked
    if not intake_configured():
        return
    linked = {(c.get("metadata") or {}).get("product_card"): c for c in pipeline if (c.get("metadata") or {}).get("product_card")}
    mine = product_cards_mine()
    by = {}
    for c in mine:
        by.setdefault(product_col(c["list_id"]), []).append(c)
    limit = None if a.product else 5
    print(f"PRODUCT board, assigned to you  ({len(mine)} cards" + ("" if a.product else "; 5 most recent per column, `pl list --product` for all") + ")")
    for col in C.PRODUCT_COLUMNS:
        if col == "Done" and not a.all:
            continue
        rows = sorted(by.get(col, []), key=lambda c: c.get("updated_at") or "", reverse=True)
        print(f"{col}  ({len(rows)})")
        for c in rows[:limit]:
            f = linked.get(c["id"])
            mark = f"funnel {f['id'][:8]}:{col_name(f['list_id'])}" if f else ("in funnel" if (c.get("metadata") or {}).get("pipeline_card") else "")
            tags = ",".join(t for t in (c.get("tags") or []) if t in ("p0", "p1", "p2", "p3", "p4", "security", "bug", "feature", *(C.INTAKE.get("repo_tags") or [])))[:28]
            print(f"  {c['id'][:8]}  {c['title'][:56]:<56}  {tags:<28} {age(parse_iso(c.get('updated_at') or '')):>4}  {mark}")
        if limit and len(rows) > limit:
            print(f"  ... {len(rows) - limit} more")
    print("\npull one into the funnel: pl pull <id>")


def plan_path(c):
    m = c.get("metadata") or {}
    p = Path(m["plan_path"]).expanduser() if m.get("plan_path") else None
    try:   # plan_path is tracker data: only a file inside the plans folder is ever read or written
        inside = bool(p) and p.resolve().is_relative_to(C.PLANS.resolve())
    except (OSError, ValueError):
        inside = False
    if inside and p.exists():
        return p
    fallback = C.PLANS / f"{slug_of(c)}.md"   # the slug is tracker data too: it must stay inside the plans folder
    try:
        if fallback.resolve().is_relative_to(C.PLANS.resolve()):
            return fallback
    except (OSError, ValueError):
        pass
    raise SystemExit(f"pl: card {c['id'][:8]} has a plan path or slug outside the plans folder; nothing was read or written")


def pull_plan(c):
    sec = sections(c.get("description")).get("PLAN")
    if sec is None:
        raise SystemExit(f"pl: card {c['id'][:8]} has no '# PIPELINE: PLAN' section yet ({col_name(c['list_id'])})")
    path = plan_path(c)
    if path.exists() and path.stat().st_mtime > parse_iso(c.get("updated_at") or "") \
            and path.read_text().strip() != sec.strip():
        return path  # your local edits are newer than the card; keep them
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(sec.rstrip() + "\n")
    pulls = _plan_pulls()
    pulls[str(path)] = {"card": c["id"], "sha": _sha(path.read_text())}
    _pulls_file().write_text(json.dumps(pulls))
    return path


def _pulls_file():
    return C.STATE_DIR / "pl-plan-pulls.json"


def _plan_pulls():
    """{plan file: {card, sha}}: what pl last wrote to each local plan file, so approve can tell your edits apart."""
    try:
        return json.loads(_pulls_file().read_text())
    except (OSError, ValueError):
        return {}


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def edited_plan(c, path):
    """The local plan text when you edited it after pl pulled it for this card, else None (the card is the truth).
    Split parts share one plan file, so a file pl never pulled, or pulled for another card, is never uploaded."""
    if not path.exists():
        return None
    text = path.read_text()
    rec = _plan_pulls().get(str(path)) or {}
    if rec.get("card") != c["id"] or rec.get("sha") == _sha(text):
        return None
    return text if text.strip() != (sections(c.get("description")).get("PLAN") or "").strip() else None


NVIM_WRAP = "setlocal wrap linebreak breakindent"  # long plan paragraphs wrap at word boundaries instead of running off-screen


def open_in_nvim(path, here=False):
    """Open the plan in Neovim in a NEW iTerm window (its own OS window), or right here with here=True."""
    if here and sys.stdin.isatty() and not os.environ.get("TMUX"):
        os.execvp("nvim", ["nvim", "-c", NVIM_WRAP, str(path)])
    if here and os.environ.get("TMUX"):
        tmux("new-window", "-n", "plan-review", f"nvim -c '{NVIM_WRAP}' {path}")
        return
    # A login+interactive zsh so nvim sees the normal PATH and plugins; the window closes when nvim quits.
    cmd = f"/usr/bin/env DISABLE_AUTO_UPDATE=true /bin/zsh -lic 'nvim -c \"{NVIM_WRAP}\" {path}'"
    r = subprocess.run(["osascript", "-", cmd], input=NEW_WINDOW_SCRIPT, capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"pl: could not open an iTerm window ({r.stderr.strip()[:120]}); open it yourself: nvim {path}")
    print(f"opened {path.name} in a new iTerm window")


def cmd_review(a):
    if a.what and re.fullmatch(r"[0-9a-f-]{8,}", a.what):
        return open_in_nvim(pull_plan(find_card(a.what)), a.here)
    if a.what:
        p = Path(a.what).expanduser()
        p = p if p.is_file() else C.PLANS / (a.what if a.what.endswith(".md") else f"{a.what}.md")
        if not p.is_file():
            raise SystemExit(f"pl: no plan {a.what} in {C.PLANS}")
        return open_in_nvim(p, a.here)
    files = sorted(C.PLANS.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        raise SystemExit(f"pl: no plans in {C.PLANS}")
    open_in_nvim(files[0], a.here)


def spec_gated(col):
    return col == "Spec ready" and bool((C.GATES or {}).get("spec"))


def cmd_approve(a):
    fresh_next()   # it writes what it read: never from a copy that may be minutes old
    c = find_card(a.id)
    col = col_name(c["list_id"])
    if spec_gated(col):
        at = (c.get("metadata") or {}).get("spec_approved_at")
        if at:
            print(f"already approved at {datetime.fromtimestamp(parse_iso(at)).strftime('%H:%M')} \u2014 the planner starts on its own")
            return
        update(c["id"], metadata={"spec_approved_at": now_iso(), "spec_approved_by": C.USER_EMAIL})
        events.emit("spec_approved", c["id"])
        print(f"spec approved {c['id'][:8]}  {c['title']}\n  the dispatcher starts the plan (or design) on its next pass")
        return
    if col != "Plan for review" and not a.force:
        raise SystemExit(f"pl approve: card is in '{col}', not 'Plan for review' (use --force to override)")
    parts = sections(c.get("description"))
    path = plan_path(c)
    edited = edited_plan(c, path)
    if edited is not None:
        parts["PLAN"] = edited
        try:
            desc = check_size(render(parts))
        except SystemExit as e:
            raise SystemExit(f"pl approve: your local plan {path} is bigger than this card's part; nothing was "
                             f"written. Trim it to this card's work packages, then approve again ({e})")
        update(c["id"], description=desc)
        print(f"synced your edits from {path} to the card")
    update(c["id"], list_id=col_id("Approved"),
           metadata={"approved_by": C.USER_EMAIL, "approved_at": now_iso(), "worker": None})
    events.emit("approved", c["id"])
    print(f"approved {c['id'][:8]}  {c['title']}\n  the dispatcher starts the run on its next pass")
    notify(f"Approved: {c['title'][:50]}", "the run starts on the next dispatcher pass")


def cmd_reject(a):
    fresh_next()   # it rebuilds the whole body from what it read: never from a copy that may be minutes old
    c = find_card(a.id)
    parts = sections(c.get("description"))
    m = c.get("metadata") or {}
    if spec_gated(col_name(c["list_id"])):
        w = m.get("worker")
        if w and worker_status(w, registry())[0] != "dead":
            raise SystemExit("pl reject: an agent is working on this card; wait or stop it first")
        n = int(m.get("spec_round") or 0) + 1
        parts["INPUT"] = (parts.get("INPUT") or "").rstrip() + f"\n\n## Spec review notes {now_iso()[:10]} round {n}\n{a.notes.strip()}"
        update(c["id"], description=check_size(render(parts)))
        update(c["id"], list_id=col_id("Inbox"), metadata={"spec_round": n, "worker": None, "spec_approved_at": None})
        events.emit("spec_rejected", c["id"])
        print(f"sent back {c['id'][:8]}  {c['title']}\n  back in Inbox with your notes; the spec writer rewrites it on the next pass")
        return
    n = int(m.get("review_round") or 0) + 1
    entry = f"## {now_iso()[:10]} round {n}\n{a.notes.strip()}"
    parts["REVIEW NOTES"] = (parts["REVIEW NOTES"].rstrip() + "\n\n" + entry) if parts.get("REVIEW NOTES") else entry
    update(c["id"], description=check_size(render(parts)))
    update(c["id"], list_id=col_id("Spec ready"), metadata={"review_round": n, "worker": None})
    events.emit("rejected", c["id"])
    print(f"rejected {c['id'][:8]}  {c['title']}\n  back in Spec ready with your notes; the planner re-plans on the next pass")


def cmd_done(a):
    """A human's call: move a Feature Pipeline card, or a Product card, to Done."""
    hits = [c for c in cards() if c["id"].startswith(a.id)]
    if hits:
        c = card(hits[0]["id"])
        update(c["id"], list_id=col_id("Done"), metadata={"done_by": C.USER_EMAIL, "done_at": now_iso(), "worker": None})
        m = c.get("metadata") or {}
        if m.get("product_card") and intake_configured() and "Done" in product_lists():
            try:
                trackers.get("intake").update(m["product_card"], verify=False, list_id=product_lists()["Done"])
                print(f"  linked Product card {m['product_card'][:8]} moved to Done as well")
            except SystemExit as e:   # the pipeline card is done either way
                print(f"  linked Product card {m['product_card'][:8]} not moved ({e}); move it by hand")
        print(f"done {c['id'][:8]}  {c['title']}  (pipeline board, was in {col_name(c['list_id'])})")
        return
    pc = product_card(a.id) if len(a.id) >= 32 else None
    if pc is None:
        ms = [c for c in product_cards_mine() if c["id"].startswith(a.id)]
        if len(ms) != 1:
            raise SystemExit(f"pl done: {len(ms)} cards match {a.id!r} on either board")
        pc = product_card(ms[0]["id"])
    body = {"list_id": product_lists()["Done"],
            "metadata": {**(pc.get("metadata") or {}), "done_by": C.USER_EMAIL, "done_at": now_iso()}}
    if a.note:
        body["description"] = check_size((pc.get("description") or "").rstrip() + f"\n\n## Closed {now_iso()[:10]}\n{a.note.strip()}\n")
    got = trackers.get("intake").update(pc["id"], verify=False, **body)
    if got.get("list_id") != product_lists()["Done"]:
        raise SystemExit(f"pl done: move did not persist for {pc['id'][:8]}")
    print(f"done {pc['id'][:8]}  {pc['title'][:70]}  (Product board, was in {product_col(pc['list_id'])})")



def cmd_move(a):
    """Move a pipeline card to a column. On a GitHub Project this reads the one issue, never the whole board."""
    t = trackers.get("tracker")
    if hasattr(t, "move"):
        cid = t.move(a.id, a.column)
    else:
        cid = find_card(a.id)["id"]
        t.update(cid, column=a.column)
    print(f"moved {cid} to {a.column}")


def cmd_profiles(a):
    st = profile_state()
    if a.reset:
        for p in (list(st) if a.reset == "all" else [a.reset]):
            st.pop(p, None)
        unpark_machine(list(C.PROFILES) if a.reset == "all" else [a.reset])
        C.ATTN.mkdir(exist_ok=True)
        C.PROFILE_STATE.write_text(json.dumps(st, indent=1))
        print(f"un-parked {a.reset}")
    bad = exhausted_profiles()
    for p in C.PROFILES:
        r = st.get(p) or {}
        state = f"PARKED until {r.get('until', '')[11:16]} UTC ({r.get('reason', '')}, seen {r.get('exhausted_at', '')[:16]})" if p in bad else "ok"
        print(f"  {p:<6} {state:<70} {C.PROFILES[p]}")


def cmd_card(a):
    c = find_card(a.id)
    if a.delete:
        trackers.get("tracker").delete(c["id"])
        print(f"deleted {c['id'][:8]}  {c['title']}")
        return
    print(f"{c['id']}  {c['title']}\ncolumn: {col_name(c['list_id'])}   tags: {c.get('tags')}   updated: {c.get('updated_at')}")
    print("metadata: " + json.dumps(c.get("metadata") or {}, indent=1))
    for k, v in sections(c.get("description")).items():
        print(f"-- section {k or '(preamble)'}: {len(v)} chars, first line: {v.strip().splitlines()[0][:80] if v.strip() else ''}")


def cmd_board(a):
    if a.action != "init":
        raise SystemExit("pl board: only 'init' is supported")
    if "Inbox" in lists():
        print(f"Inbox exists: {lists()['Inbox']['id']}")
        return
    try:
        lid = trackers.get("tracker").ensure_column("Inbox", description=(
            "Raw ideas dropped by `pl idea` (or by a teammate in the UI). Body: `# PIPELINE: INPUT` with the idea and "
            "inlined documents. `pl dispatch` starts the spec stage's prompt on each card here; the spec agent moves it to Spec ready."))
    except SystemExit as e:
        raise SystemExit("pl board init: create failed: " + str(e).split(" failed: ", 1)[-1])
    print(f"created Inbox: {lid}")


def cmd_pull(a):
    if a.id:
        pc = product_card(a.id if len(a.id) >= 32 else a.id)
        why = pull_reason_to_skip(pc, linked_product_ids(cards()))
        if why:
            raise SystemExit(f"pl pull: {pc['title'][:50]}: {why}")
        pull_one(pc, cards(), a.dry_run)
        seen = load_seen() or set()
        if not a.dry_run:
            C.SEEN_FILE.write_text(json.dumps(sorted(seen | {pc['id']})))
        return
    if a.list:
        linked = linked_product_ids(cards())
        seen = load_seen() or set()
        rows = [c for c in product_cards_mine() if product_col(c["list_id"]) in C.PRODUCT_INTAKE_COLUMNS]
        print(f"Product cards assigned to you in {', '.join(C.PRODUCT_INTAKE_COLUMNS)}: {len(rows)}   (new = not in the snapshot)")
        for c in sorted(rows, key=lambda c: c.get("updated_at") or "", reverse=True):
            why = pull_reason_to_skip(c, linked)
            flag = why or ("NEW" if c["id"] not in seen else "known")
            print(f"  {c['id'][:8]}  {c['title'][:62]:<62}  {product_col(c['list_id']):<8} {flag}")
        print("\npull one by hand: pl pull <id>")
        return
    pull_new(a.dry_run)


def cmd_adopt(a):
    c = find_card(a.id)
    m = c.get("metadata") or {}
    if m.get("pipeline_mode") == "auto":
        print(f"{c['id'][:8]} is already a funnel card (profile {m.get('profile')})")
        return
    update(c["id"], metadata={"pipeline_mode": "auto", "profile": a.account or m.get("profile") or next_profile(cards()),
                              "adopted_at": now_iso(), "adopted_by": C.USER_EMAIL})
    print(f"adopted {c['id'][:8]}  {c['title']}  ({col_name(c['list_id'])}); the dispatcher takes it from here")


def retry(ref, stage=None):
    """Clear the worker (and its attempt count) of one card, or with ref "all" of every funnel card whose agent hit the
    attempt limit, so the dispatcher starts it fresh. A running agent is left alone. Returns one line per card."""
    fresh_next()   # it resets what it read: never a worker restarted since a cached copy
    cs = cards()
    if ref == "all":
        picked = [c for c in cs if (c.get("metadata") or {}).get("pipeline_mode") == "auto"
                  and int(((c.get("metadata") or {}).get("worker") or {}).get("attempts") or 0) >= MAX_ATTEMPTS]
    else:
        n = ref[1:] if ref.startswith("#") else ""   # like find_card: the full id, an 8+ char prefix, or #n
        picked = [c for c in cs if c["id"] == ref] or [
            c for c in cs if (len(ref) >= 8 and c["id"].startswith(ref)) or (n.isdigit() and c["id"].endswith(f"#{n}"))]
        if len(picked) != 1:
            raise SystemExit(f"pl retry: {len(picked)} cards match {ref!r}")
    reg, out = registry(), []
    for c in picked:
        w = (c.get("metadata") or {}).get("worker") or {}
        head = f"{c['id'][:8]}  {c['title'][:50]}"
        if not w or (stage and w.get("stage") != stage):
            out.append(f"{head}: no {stage or ''} agent to reset".replace("  agent", " agent"))
            continue
        status = worker_status(w, reg)[0]
        if status in ("alive", "starting"):
            if ref != "all":
                out.append(f"{head}: its {w.get('stage')} agent is running; nothing reset")
            continue
        update(c["id"], metadata={"worker": None})
        out.append(f"{head}: reset the {w.get('stage')} agent ({int(w.get('attempts') or 0)} attempts); the dispatcher starts it fresh")
    return out or ["no card hit the attempt limit"]


def cmd_retry(a):
    for line in retry(a.id, a.stage):
        print(line)


def cmd_pause(a):
    if paused():
        print(f"already paused since {paused().get('since', '?')[:16]} UTC")
        return
    C.ATTN.mkdir(exist_ok=True)
    C.PAUSE_FILE.write_text(json.dumps({"since": now_iso(), "host": C.HOST}))
    print("paused: no new agents start from the next dispatcher pass; working agents finish their step (pl resume)")


def cmd_resume(a):
    if not paused():
        print("not paused")
        return
    C.PAUSE_FILE.unlink()
    print("resumed: the next dispatcher pass starts waiting cards again")


PR_URL = re.compile(r"https://github\.com/([^/]+)/([^/]+)/pull/(\d+)")


def cmd_intent(a):
    """Print what a PR was supposed to do, for reviewers: the spec and the plan's scope sections of the pipeline
    card behind it, plus any other tracker card its body links. Always exits 0; says so when nothing is found."""
    m = PR_URL.match(a.pr)
    if not m:
        raise SystemExit("pl intent: give the full PR URL, https://github.com/<owner>/<repo>/pull/<n>")
    r = subprocess.run(["gh", "pr", "view", m.group(3), "--repo", f"{m.group(1)}/{m.group(2)}", "--json", "body"],
                       capture_output=True, text=True, timeout=60, env=C.gh_env())
    body = (json.loads(r.stdout).get("body") or "") if r.returncode == 0 else ""
    print(intent_text(a.pr, body))


def intent_text(pr, body):
    """The `pl intent` text for PR URL pr whose body is body: the spec + plan scope of the cards behind it."""
    ids = [c["id"] for c in cards() if pr in ((c.get("metadata") or {}).get("pr_urls") or [])]
    ids = list(dict.fromkeys(ids + re.findall(r"cardId=([0-9a-f-]{36})", body)))
    out = [f"# What {pr} was meant to do", ""]
    keep = re.compile(r"minimum change|deliberately not|acceptance|scope|non-goal", re.I)
    for cid in ids:
        try:
            c = card(cid)
        except SystemExit:
            continue
        secs = sections(c.get("description"))
        out += [f"## Card {cid[:8]}: {c['title']}", ""]
        if secs.get("SPEC"):
            out += ["### SPEC", secs["SPEC"].strip(), ""]
        if secs.get("PLAN"):
            picked = [p.strip() for p in re.split(r"(?m)^(?=## )", secs["PLAN"]) if keep.search(p.split("\n", 1)[0])]
            if picked:
                out += ["### PLAN (scope sections only)"] + picked + [""]
        if not secs.get("SPEC") and not secs.get("PLAN"):
            out += [(c.get("description") or "(no description)").strip()[:6000], ""]
    if len(out) == 2:
        out.append("No spec found: the PR body links no tracker card and no pipeline card lists this PR. "
                   "Review against the PR description only, and say so.")
    text = "\n".join(out)
    return text if len(text) <= 40000 else text[:40000] + "\n\n(truncated at 40,000 characters)"

