"""The pl subcommands (every cmd_* except dispatch and watch)."""
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from pl import config as C
from pl.accounts import check_note, exhausted_profiles, next_profile, parking, profile_state, unpark_machine
from pl.agents import NEW_WINDOW_SCRIPT, registry, worker_status, worker_view
from pl import events, ideas, move_agent, trackers
from pl.board import (card, cards, check_size, col_id, col_name, find_card, fresh_next, lists, matches, pick, render, sections,
                      share, update)
from pl.dispatch import MAX_ATTEMPTS, approved_label, busy_agents, paused
from pl.product import (linked_product_ids, load_seen, product_card, product_cards_mine, product_col, product_lists,
                        intake_configured, pull_new, pull_one, pull_reason_to_skip)
from pl.util import age, notify, now_iso, parse_iso, short_id, slug_of, tmux


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
    print(f"created {item['id'] if direct else short_id(item['id'])}  {title}\n  {where}, {len(docs)} document(s)\n  {t.url(item['id'])}")
    print("  the dispatcher writes its spec on its next pass" if direct
          else "  assigned to you: the pipeline pulls it in within a few minutes" if a.start
          else "  unassigned, waiting to be assessed; assign it to yourself (or rerun with --start) to start the pipeline")



def cmd_list(a):
    bad = exhausted_profiles()
    if bad:
        print("PROFILES parked (out of usage credits): " + ", ".join(f"{p} until {(parking(p).get('until') or '')[11:16]} UTC"
                                                                 for p in sorted(bad)) + "   (pl accounts)")
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
                print(f"  {short_id(c['id'])}  {c['title'][:56]:<56}  {approved_label(c, col, reg) or worker_view(c, reg):<28} {age(parse_iso(c.get('updated_at') or ''))}")
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
            mark = f"funnel {short_id(f['id'])}:{col_name(f['list_id'])}" if f else ("in funnel" if (c.get("metadata") or {}).get("pipeline_card") else "")
            tags = ",".join(t for t in (c.get("tags") or []) if t in ("p0", "p1", "p2", "p3", "p4", "security", "bug", "feature", *(C.INTAKE.get("repo_tags") or [])))[:28]
            print(f"  {short_id(c['id'])}  {c['title'][:56]:<56}  {tags:<28} {age(parse_iso(c.get('updated_at') or '')):>4}  {mark}")
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
    raise SystemExit(f"pl: card {short_id(c['id'])} has a plan path or slug outside the plans folder; nothing was read or written")


def pull_plan(c):
    sec = sections(c.get("description")).get("PLAN")
    if sec is None:
        raise SystemExit(f"pl: card {short_id(c['id'])} has no '# PIPELINE: PLAN' section yet ({col_name(c['list_id'])})")
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
    if a.what and re.fullmatch(r"[0-9a-f-]{8,}|(?:[\w.-]+/)?[\w.-]*#\d+", a.what):   # a card id, "repo#43" or "#43"
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
        print(f"spec approved {short_id(c['id'])}  {c['title']}\n  the dispatcher starts the plan (or design) on its next pass")
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
    print(f"approved {short_id(c['id'])}  {c['title']}\n  the dispatcher starts the run on its next pass")
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
        print(f"sent back {short_id(c['id'])}  {c['title']}\n  back in Inbox with your notes; the spec writer rewrites it on the next pass")
        return
    n = int(m.get("review_round") or 0) + 1
    entry = f"## {now_iso()[:10]} round {n}\n{a.notes.strip()}"
    parts["REVIEW NOTES"] = (parts["REVIEW NOTES"].rstrip() + "\n\n" + entry) if parts.get("REVIEW NOTES") else entry
    update(c["id"], description=check_size(render(parts)))
    update(c["id"], list_id=col_id("Spec ready"), metadata={"review_round": n, "worker": None})
    events.emit("rejected", c["id"])
    print(f"rejected {short_id(c['id'])}  {c['title']}\n  back in Spec ready with your notes; the planner re-plans on the next pass")


def cmd_done(a):
    """A human's call: move a Feature Pipeline card, or a Product card, to Done."""
    hits = matches(a.id, cards())
    if hits:
        for line in done(a.id, hits):
            print(line)
        return
    pc = product_card(a.id) if len(a.id) >= 32 else None
    if pc is None:
        ms = matches(a.id, product_cards_mine())
        if len(ms) != 1:
            raise SystemExit(f"pl done: {len(ms)} cards match {a.id!r} on either board")
        pc = product_card(ms[0]["id"])
    body = {"list_id": product_lists()["Done"],
            "metadata": {**(pc.get("metadata") or {}), "done_by": C.USER_EMAIL, "done_at": now_iso()}}
    if a.note:
        body["description"] = check_size((pc.get("description") or "").rstrip() + f"\n\n## Closed {now_iso()[:10]}\n{a.note.strip()}\n")
    got = trackers.get("intake").update(pc["id"], verify=False, **body)
    if got.get("list_id") != product_lists()["Done"]:
        raise SystemExit(f"pl done: move did not persist for {short_id(pc['id'])}")
    print(f"done {short_id(pc['id'])}  {pc['title'][:70]}  (Product board, was in {product_col(pc['list_id'])})")


def done(ref, cs=None):
    """A person's call: move one Feature Pipeline card (and its linked Product card) to Done. cs: the cards to pick
    from, default every pipeline card. A live agent is not stopped. Returns lines to show."""
    c = card(pick(ref, cards() if cs is None else cs, "pl done")["id"])   # two matches: refused, never the first
    update(c["id"], list_id=col_id("Done"), metadata={"done_by": C.USER_EMAIL, "done_at": now_iso(), "worker": None})
    m, out = c.get("metadata") or {}, []
    if m.get("product_card") and intake_configured() and "Done" in product_lists():
        try:
            trackers.get("intake").update(m["product_card"], verify=False, list_id=product_lists()["Done"])
            out.append(f"  linked Product card {short_id(m['product_card'])} moved to Done as well")
        except SystemExit as e:   # the pipeline card is done either way
            out.append(f"  linked Product card {short_id(m['product_card'])} not moved ({e}); move it by hand")
    return out + [f"done {short_id(c['id'])}  {c['title']}  (pipeline board, was in {col_name(c['list_id'])})"]


DROP_KEYS = ("dropped_at", "dropped_by", "drop_reason", "dropped_from")


def drop(ref, reason=None):
    """A card no longer needed: stop its live agent, move it to Done marked dropped (not done), and its linked
    Product card to Done too. Refuses when the agent cannot be stopped. Returns lines to show."""
    fresh_next()   # it writes what it read: never from a copy that may be minutes old
    c = card(pick(ref, cards(), "pl drop")["id"])   # two matches: refused, never the first
    m, col, head = c.get("metadata") or {}, col_name(c["list_id"]), f"{short_id(c['id'])}  {c['title'][:50]}"
    if col == "Done":
        raise SystemExit(f"pl drop: {head} is already in Done" + (" (dropped)" if m.get("dropped_at") else ""))
    w, out = m.get("worker") or {}, []
    live = bool(w) and worker_status(w, registry())[0] in ("alive", "starting")
    if live and not move_agent.lock(c["id"]):   # the move lock also keeps the dispatcher off the card meanwhile
        raise SystemExit(f"pl drop: {head}: an agent move of this card is in progress; not dropped, try again")
    try:
        if live:
            why = move_agent.pane_refusal(c, w)
            if why or not move_agent.stop(w["pane"], w.get("session_id")):
                raise SystemExit(f"pl drop: {head}: its {w.get('stage')} agent could not be stopped "
                                 f"({why or 'still running after Ctrl-C'}); not dropped. Stop it in its window, then drop again")
            out.append(f"  stopped its {w.get('stage')} agent (Ctrl-C in its window)")
        meta = {"dropped_at": now_iso(), "dropped_by": C.USER_EMAIL, "drop_reason": reason, "dropped_from": col, "worker": None}
        if w.get("window"):   # the dispatcher closes a finished worker's window once its session is gone
            meta["finished_workers"] = list(m.get("finished_workers") or []) + [w]
        update(c["id"], list_id=col_id("Done"), metadata=meta)
    finally:
        if live:
            move_agent.unlock(c["id"])
    events.emit("dropped", c["id"], **{"from": col})   # the reason is card text: never in the event log
    if m.get("product_card") and intake_configured() and "Done" in product_lists():
        try:
            trackers.get("intake").update(m["product_card"], verify=False, list_id=product_lists()["Done"])
            out.append(f"  linked Product card {short_id(m['product_card'])} moved to Done as well")
        except SystemExit as e:   # the pipeline card is dropped either way
            out.append(f"  linked Product card {short_id(m['product_card'])} not moved ({e}); move it by hand")
    return [f"dropped {head}  (moved to Done, was in {col}; pl undrop {short_id(c['id'])} puts it back)"] + out


def undrop(ref):
    """Put a dropped card back in the column it was dropped from (Inbox when that is unknown) and clear the drop.
    A linked Product card stays where it is. Returns lines to show."""
    fresh_next()
    c = card(pick(ref, cards(), "pl undrop")["id"])   # cards() lists Done cards too
    m, head = c.get("metadata") or {}, f"{short_id(c['id'])}  {c['title'][:50]}"
    if not m.get("dropped_at"):
        raise SystemExit(f"pl undrop: {head} was not dropped")
    if col_name(c["list_id"]) != "Done":
        raise SystemExit(f"pl undrop: {head} is not in Done any more (it is in {col_name(c['list_id'])}); move it with pl move")
    back = m.get("dropped_from") if m.get("dropped_from") in C.COLUMNS and m.get("dropped_from") != "Done" else "Inbox"
    update(c["id"], list_id=col_id(back), metadata=dict.fromkeys(DROP_KEYS))
    events.emit("undropped", c["id"], to=back)
    out = [f"undropped {head}  (back in {back})"]
    if m.get("product_card"):
        out.append(f"  linked Product card {short_id(m['product_card'])} stays where it is; move it by hand if needed")
    return out


def cmd_drop(a):
    for line in drop(a.id, a.reason):
        print(line)


def cmd_undrop(a):
    for line in undrop(a.id):
        print(line)



PR_URL_RE = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")


def cmd_move(a):
    """Move a pipeline card to a column. On a GitHub Project this reads the one issue, never the whole board.
    --pr URL first records the pull request in metadata.pr_urls (once), from a fresh read, then moves."""
    pr = getattr(a, "pr", None)
    if pr is not None and not PR_URL_RE.fullmatch(pr):
        raise SystemExit(f"pl move: --pr {pr!r} is not a GitHub pull request URL (https://github.com/OWNER/REPO/pull/N)")
    cid = None
    if pr is not None:   # recorded before the move: the move tells the dispatcher the run is done
        fresh_next()     # it writes what it read: never from a copy that may be minutes old
        t = trackers.get("tracker")
        cid = t.ref_id(a.id) if hasattr(t, "move") else find_card(a.id)["id"]
        urls = list((card(cid).get("metadata") or {}).get("pr_urls") or [])
        if pr not in urls:
            update(cid, metadata={"pr_urls": urls + [pr]})
        print(f"recorded {pr}")
    print(f"moved {move_to(a.id, a.column, cid)} to {a.column}")


def move_to(ref, column, cid=None):
    """Move one pipeline card to a column; returns its id. cid: the id when the caller already resolved it."""
    t = trackers.get("tracker")
    if hasattr(t, "move"):
        return t.move(ref, column)
    cid = cid or find_card(ref)["id"]
    t.update(cid, column=column)
    return cid


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
        r = parking(p)   # with the machine-wide entry: parked by another profile that shares the folder, and its checks
        state = f"PARKED until {r.get('until', '')[11:16]} UTC ({r.get('reason', '')}, seen {r.get('exhausted_at', '')[:16]})" if p in bad else "ok"
        print(f"  {p:<6} {state:<70} {C.PROFILES[p]}")
        if p in bad:
            print(f"         {check_note(p)}")


def cmd_card(a):
    c = find_card(a.id)
    if a.delete:
        trackers.get("tracker").delete(c["id"])
        print(f"deleted {short_id(c['id'])}  {c['title']}")
        return
    print(f"{c['id']}  {c['title']}\ncolumn: {col_name(c['list_id'])}   tags: {c.get('tags')}   updated: {c.get('updated_at')}")
    print("metadata: " + json.dumps(c.get("metadata") or {}, indent=1))
    for k, v in sections(c.get("description")).items():
        print(f"-- section {k or '(preamble)'}: {len(v)} chars, first line: {v.strip().splitlines()[0][:80] if v.strip() else ''}")


WRITABLE = ("SPEC", "DESIGN", "PLAN")   # the sections stage agents write; INPUT and REVIEW NOTES are yours
SECTION_MAX = 256 * 1024                # bytes a --from file may hold
APPROVED_COLUMNS = ("Approved", "In progress", "PR open", "Done")   # the PLAN is a person's decision from here on
AGENT_DIRS = ("skills", "agents", "commands")   # the only parts of a harness folder --from may read


def _section_source(src):
    """The text of a --from file: never a credential file (by name, through a link, or as a hard link), nothing in a
    harness account folder outside skills/, agents/ and commands/, nothing over SECTION_MAX."""
    from pl import harnesses
    if src == "-":
        raw = sys.stdin.buffer.read(SECTION_MAX + 1)
    else:
        p = Path(src).expanduser()
        try:
            r = p.resolve(strict=True)
            st = os.stat(r)
        except (OSError, RuntimeError) as e:
            raise SystemExit(f"pl section: cannot read {src}: {e}") from None
        if harnesses.SECRET_RE.fullmatch(p.name) or harnesses.SECRET_RE.fullmatch(r.name) \
                or not stat.S_ISREG(st.st_mode) or harnesses.is_secret_file(None, st):
            raise SystemExit(f"pl section: refused: {src} is a credential file or not a plain file")
        for root in harnesses._roots():
            if r.is_relative_to(root) and r.relative_to(root).parts[0] not in AGENT_DIRS:
                raise SystemExit(f"pl section: refused: {src} is inside a harness account folder")
        if st.st_size > SECTION_MAX:
            raise SystemExit(f"pl section: {src} is over 256 KB")
        fd = os.open(r, os.O_RDONLY | os.O_NOFOLLOW)   # a link swapped in after the checks is not followed
        with os.fdopen(fd, "rb") as f:
            raw = f.read(SECTION_MAX + 1)
    if len(raw) > SECTION_MAX:
        raise SystemExit("pl section: the text is over 256 KB")
    if b"\0" in raw:
        raise SystemExit("pl section: the text has a NUL byte; nothing was written")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise SystemExit("pl section: the text is not UTF-8") from None


def cmd_section(a):
    """pl section <id> NAME: print one section in full. --from FILE (- for stdin): replace it with the file's text.
    An approved PLAN or SPEC is refused unless --force (a person's yes)."""
    name = " ".join(a.name.split()).upper()
    if a.from_ is None:
        text = sections(find_card(a.id).get("description")).get(name)
        if text is None:
            print(f"pl: card {short_id(a.id)} has no {name} section", file=sys.stderr)
        else:
            print(text.rstrip("\n"))
        return
    if name not in WRITABLE:
        raise SystemExit(f"pl section: only SPEC, DESIGN or PLAN can be written, not {name}")
    text = _section_source(a.from_)
    if not text.strip():
        raise SystemExit("pl section: the text is empty; nothing was written")
    if re.search(r"^# PIPELINE:", text, re.M):
        raise SystemExit("pl section: the text has a line starting with '# PIPELINE:', which would split the card")
    fresh_next()   # it rebuilds the whole body from what it read: never from a copy that may be minutes old
    c = find_card(a.id)
    if not getattr(a, "force", False):
        if name == "PLAN" and (col := col_name(c.get("list_id"))) in APPROVED_COLUMNS:
            raise SystemExit(f"pl section: the plan was approved (card is in '{col}'); a person can override with --force")
        if name == "SPEC" and (c.get("metadata") or {}).get("spec_approved_at"):
            raise SystemExit("pl section: the spec was approved; a person can override with --force")
    parts = sections(c.get("description"))
    parts[name] = text.strip("\n")
    update(c["id"], description=check_size(render(parts)))
    print(f"wrote {name} ({len(text.strip())} chars) to card {short_id(c['id'])}")


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
            print(f"  {short_id(c['id'])}  {c['title'][:62]:<62}  {product_col(c['list_id']):<8} {flag}")
        print("\npull one by hand: pl pull <id>")
        return
    pull_new(a.dry_run)


def cmd_adopt(a):
    c = find_card(a.id)
    m = c.get("metadata") or {}
    if m.get("pipeline_mode") == "auto":
        print(f"{short_id(c['id'])} is already a funnel card (profile {m.get('profile')})")
        return
    update(c["id"], metadata={"pipeline_mode": "auto", "profile": a.account or m.get("profile") or next_profile(cards()),
                              "adopted_at": now_iso(), "adopted_by": C.USER_EMAIL})
    print(f"adopted {short_id(c['id'])}  {c['title']}  ({col_name(c['list_id'])}); the dispatcher takes it from here")


def retry(ref, stage=None):
    """Clear the worker (and its attempt count) of one card, or with ref "all" of every funnel card whose agent hit the
    attempt limit, so the dispatcher starts it fresh. A running agent is left alone. Returns one line per card."""
    fresh_next()   # it resets what it read: never a worker restarted since a cached copy
    cs = cards()
    if ref == "all":
        picked = [c for c in cs if (c.get("metadata") or {}).get("pipeline_mode") == "auto"
                  and int(((c.get("metadata") or {}).get("worker") or {}).get("attempts") or 0) >= MAX_ATTEMPTS]
    else:
        picked = [pick(ref, cs, "pl retry", min_prefix=8)]   # like find_card, but a prefix needs 8+ chars
    reg, out = registry(), []
    for c in picked:
        w = (c.get("metadata") or {}).get("worker") or {}
        head = f"{short_id(c['id'])}  {c['title'][:50]}"
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
        out += [f"## Card {short_id(cid)}: {c['title']}", ""]
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

