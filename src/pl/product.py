"""Intake board bridge: cards assigned to you there become funnel ideas."""
import json

from pl import config as C
from pl import trackers
from pl.accounts import healthy_profile, next_profile
from pl.board import MARK, cards, check_size, render, update
from pl.util import card_url, notify, now_iso


def _t():
    return trackers.get("intake")


def product_lists():
    return _t().columns()


def product_col(list_id):
    return next((t for t, i in product_lists().items() if i == list_id), "?")


def product_url(item_id):
    return _t().url(item_id)


def is_mine(c):
    ids = {v for v in (C.USER_UUID, C.USER_EMAIL, C.USER_LOGIN) if v}
    return c.get("assigned_to") in ids or bool(C.USER_NAME) and (c.get("assigned_to_display") or "") == C.USER_NAME


def product_cards_mine():
    """Intake cards assigned to you. The tracker may ignore the assigned_to query, so the filter is client-side."""
    if not (C.USER_UUID or C.USER_EMAIL):
        raise SystemExit(f"pl: set [user] email (or uuid) in {C.path() or 'config.toml'}: the intake bridge pulls the cards assigned to you")
    return [c for c in _t().cards({"assigned_to": C.USER_UUID or C.USER_EMAIL}) if is_mine(c)]


def intake_configured():
    return bool((C.INTAKE or {}).get("type"))


def product_card(item_id):
    try:
        return _t().card(item_id)
    except SystemExit as e:
        if str(e).startswith("pl: no card with id"):
            raise SystemExit(f"pl pull: no Product card with id {item_id}")
        raise


def linked_product_ids(all_cards):
    return {(c.get("metadata") or {}).get("product_card") for c in all_cards if (c.get("metadata") or {}).get("product_card")}


def pull_reason_to_skip(pc, linked):
    m = pc.get("metadata") or {}
    if m.get("pipeline_card") or pc["id"] in linked:
        return "already in the funnel"
    if C.PRODUCT_SKIP_TAGS & set(pc.get("tags") or []):
        return "tagged " + ",".join(sorted(C.PRODUCT_SKIP_TAGS & set(pc.get("tags") or [])))
    return None


def pull_one(pc, all_cards, dry=False):
    """Create the funnel card for one Product card and link both ways."""
    col = product_col(pc["list_id"])
    tags = pc.get("tags") or []
    repos = [t for t in tags if t in (C.INTAKE.get("repo_tags") or [])]
    body = [f"Pulled from the Product board: {product_url(pc['id'])}",
            f"Title: {pc['title']}", f"Column: {col}", f"Tags: {', '.join(tags) or '-'}",
            f"Created: {str(pc.get('created_at'))[:10]}   Updated: {str(pc.get('updated_at'))[:10]}", "",
            "## Product card description", (pc.get("description") or "(empty)").rstrip(), "",
            "The Product card above is the source of truth for this ask. Keep it as THE Product card for the work:",
            "update it with the PR link and evidence at ship; do not create a second one."]
    profile = healthy_profile(None, all_cards) or next_profile(all_cards)
    if dry:
        print(f"  would pull {pc['id'][:8]}  {pc['title'][:60]}  ({col}, profile {profile})")
        return None
    meta = {"pipeline_mode": "auto", "profile": profile, "submitted_by": C.USER_EMAIL, "submitted_at": now_iso(),
            "submitted_from": C.HOST, "product_card": pc["id"], "product_url": product_url(pc["id"]), "product_column": col}
    desc = check_size(render({"INPUT": "\n".join(body)}))
    item = trackers.get("tracker").create("Inbox", title=pc["title"][:200], description=desc,
                                          tags=["idea", "from-product"] + repos, metadata=meta, assigned_to=trackers.me(trackers.get("tracker")))
    if not item.get("id"):
        raise SystemExit(f"pl pull: create failed: {json.dumps(item)[:300]}")
    try:
        _t().update(pc["id"], verify=False, metadata={**(pc.get("metadata") or {}), "pipeline_card": item["id"],
                                                      "pipeline_url": card_url(item["id"]), "pipeline_pulled_at": now_iso()})
    except SystemExit as e:   # an unwritable Product card never stops the pull
        print(f"  product card {pc['id'][:8]}: back-link not written ({e}); carrying on")
    print(f"  pulled {pc['id'][:8]} -> funnel {item['id'][:8]}  {pc['title'][:60]}  (profile {profile})")
    return item


def load_seen():
    try:
        v = json.loads(C.SEEN_FILE.read_text())
        return set(v) if isinstance(v, list) else None   # the issue intake's {login, seen}: a first pass here
    except Exception:
        return None


def pull_issues(dry=False, quiet=False):
    """The default intake of a github-project profile: open issues of its repo, not on the project yet, that are
    assigned to [user] login or carry the start label, are adopted onto the project in Inbox. One gh list per pass:
    GitHub search cannot OR an assignee with a label, so it lists issues off the project, newest update first (an
    assignment or a label bumps it), and filters here. Like the board bridge, assignments already there on the first
    pass (for this login) are only remembered; a labelled issue always joins, and adopting it removes the label."""
    from pl.trackers import github
    it, tr = C.ISSUE_INTAKE, trackers.get("tracker")
    listed = github.open_issues(it["repo"], f"-project:{tr.owner}/{tr.number} sort:updated-desc")
    login = (C.USER_LOGIN or "").lower()
    mine = {f"{it['repo']}#{i['number']}" for i in listed if login and login in [x.lower() for x in github._logins(i.get("assignees"))]}
    try:
        snap = json.loads(C.SEEN_FILE.read_text())
    except (OSError, ValueError):
        snap = None
    seen = set(snap["seen"]) if isinstance(snap, dict) and snap.get("login") == login else None   # a new login: a new first pass
    all_cards = cards()
    on_board = {c["id"] for c in all_cards}
    pulled = []
    for i in listed:
        cid, labels = f"{it['repo']}#{i['number']}", set(github._names(i.get("labels")))
        started = it["start_label"] in labels
        if cid in on_board or not (started or (cid in mine and seen is not None and cid not in seen)):
            continue
        if C.PRODUCT_SKIP_TAGS & labels:
            if not quiet:
                print(f"  skip {cid} {i['title'][:50]}: tagged {','.join(sorted(C.PRODUCT_SKIP_TAGS & labels))}")
            continue
        if dry:
            print(f"  would adopt {cid}  {i['title'][:60]}")
            continue
        try:
            issue = github.GitHubIssues({"repo": it["repo"]}).card(cid)
            desc = issue["description"] if MARK.search(issue["description"] or "") else check_size(render({"INPUT": "\n".join(
                [f"Adopted from the issue: {tr.url(cid)}", "", (issue["description"] or "(empty)").rstrip()])}))
            profile = healthy_profile(None, all_cards) or next_profile(all_cards)
            meta = {**issue["metadata"], "pipeline_mode": "auto", "profile": profile, "submitted_by": C.USER_EMAIL,
                    "submitted_at": now_iso(), "submitted_from": C.HOST}
            pulled.append(tr.adopt(cid, "Inbox", description=desc, metadata=meta,
                                   assigned_to=None if cid in mine else trackers.me(tr),
                                   remove_label=it["start_label"] if started else None))
        except github.RateLimited:
            raise
        except (SystemExit, Exception) as e:   # one bad issue does not stop the pass
            print(f"  could not adopt {cid}: {str(e)[:120]}")
            continue
        print(f"  adopted {cid} into the funnel  {i['title'][:60]}  (profile {profile})")
        notify(f"Pulled into the funnel: {i['title'][:45]}", "from a GitHub issue; spec agent starts next")
    if not dry:
        C.ATTN.mkdir(exist_ok=True)
        C.SEEN_FILE.write_text(json.dumps({"login": login, "seen": sorted(mine | (seen or set()))}))
    if not pulled and not quiet:
        print("issue intake: nothing new")
    return pulled


def pull_new(dry=False, quiet=False):
    """Pull Product cards NEWLY assigned to you in the intake columns. First call only seeds the snapshot."""
    if C.ISSUE_INTAKE and not intake_configured():
        return pull_issues(dry, quiet)
    if not intake_configured():
        if not quiet:
            print("product bridge: no [intake] board configured")
        return []
    mine = product_cards_mine()
    seen = load_seen()
    ids = {c["id"] for c in mine}
    if seen is None:
        if not dry:
            C.ATTN.mkdir(exist_ok=True)
            C.SEEN_FILE.write_text(json.dumps(sorted(ids)))
        print(f"product bridge: snapshot seeded with {len(ids)} cards assigned to you; only NEW assignments are pulled from now on")
        return []
    all_cards = cards()
    linked = linked_product_ids(all_cards)
    pulled = []
    for pc in mine:
        if pc["id"] in seen or product_col(pc["list_id"]) not in C.PRODUCT_INTAKE_COLUMNS:
            continue
        why = pull_reason_to_skip(pc, linked)
        if why:
            print(f"  skip {pc['id'][:8]} {pc['title'][:50]}: {why}")
            continue
        item = pull_one(pc, all_cards, dry)
        if item:
            pulled.append(item)
            notify(f"Pulled into the funnel: {pc['title'][:45]}", f"from the Product board ({product_col(pc['list_id'])}); spec agent starts next")
    if not dry:
        C.SEEN_FILE.write_text(json.dumps(sorted(ids | seen)))
    if not pulled and not quiet:
        print("product bridge: nothing new assigned to you")
    return pulled


def mirror_to_product(c, col, dry):
    """Keep the linked Product card's column in step: run started -> In Progress, PR open -> Needs Human Review."""
    m = c.get("metadata") or {}
    pid = m.get("product_card")
    if not pid or col not in ("In progress", "PR open") or m.get("product_synced_col") == col:
        return
    target = "In Progress" if col == "In progress" else "Needs Human Review"
    try:
        pc = product_card(pid)
    except SystemExit:
        return
    cur = product_col(pc["list_id"])
    ahead = ["Needs Testing", "Needs Human Review", "Done"]
    if target not in product_lists() or (cur in ahead and target == "In Progress") or cur == "Done":
        pass
    else:
        print(f"{c['id'][:8]}  product card {pid[:8]}: {cur} -> {target}")
        if not dry:
            body = {"list_id": product_lists()[target]}
            if col == "PR open" and m.get("pr_urls"):
                body["metadata"] = {**(pc.get("metadata") or {}), "pr_urls": m["pr_urls"]}
            try:
                _t().update(pid, verify=False, **body)
            except SystemExit as e:   # an unwritable Product card never stops the run
                print(f"{c['id'][:8]}  product card {pid[:8]}: write refused ({e}); carrying on")
    if not dry:
        update(c["id"], metadata={"product_synced_col": col})
