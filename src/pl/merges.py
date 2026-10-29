"""The merge judge: which open pull requests are ready to merge, in what order, and every merge rule's result.
A failed rule gets the needs-person label and one comment per head commit. The merge pass merges the ones that pass every rule."""
import fnmatch
import json
import re
import urllib.parse

from pl import agents, board, events, jsonout, judgment, merge_check, rework
from pl import config as C
from pl.board import col_name, sections
from pl.trackers import github
from pl.trackers.github import OverBudget, RateLimited
from pl.util import now_iso, short_id

SENSITIVE = ["**/migrations/**", "**/*.sql", "**/*.designer.cs", "**/schema*", "**/*auth*", "**/*token*", "**/*secret*", "**/*key*", ".github/**",
             "**/uv.lock", "**/package-lock.json", "**/yarn.lock", "**/poetry.lock", "**/Gemfile.lock", "**/go.sum", "**/Cargo.lock"]
HUMAN_TAG = "Requires Human Review"
TEST_PATH = re.compile(r"(^|/)tests?/|(^|/)test_[^/]*$|_test\.[^/]+$")
NOT_CHECKED = {"ok": None, "why": "not checked yet"}
MARK = "<!-- pl:merge-loop head={} -->"
_LABELS = set()
_STACK_CHECKS = {}


def settings():
    return {"hold": C.AUTO_MERGE.get("hold_label", "hold"), "sensitive": C.AUTO_MERGE.get("sensitive", SENSITIVE),
            "needs_person": C.AUTO_MERGE.get("needs_person_label", "needs-person")}


def method():
    return C.AUTO_MERGE.get("merge_method", "squash")


def per_pass():
    return C.AUTO_MERGE.get("max_per_pass", 5)


def account():
    """The profile's run account, whose gh sign-in merges; None when the profile has no accounts."""
    acct = (C.STAGES.get("run") or {}).get("account")
    return acct if acct in C.PROFILES else next(iter(C.PROFILES), None)


def _match(path, globs):
    path = path.lower()
    return any(fnmatch.fnmatchcase(path, g) or (g.startswith("**/") and fnmatch.fnmatchcase(path, g[3:]))
               for g in (g.lower() for g in globs))


def _pr(repo, num):
    pr = json.loads(github._gh(["pr", "view", str(num), "--repo", repo, "--json",
                                "number,url,state,isDraft,baseRefName,headRefName,headRefOid,body,labels,files,statusCheckRollup"]))
    rework.saw(pr.get("url", ""), pr.get("state"), [x["name"] for x in pr.get("labels") or []])
    return pr


def _eligible(pr):
    lab, names = rework.labels(), {x["name"] for x in pr.get("labels") or []}
    return pr.get("state") == "OPEN" and not pr.get("isDraft") and lab["ready"] in names \
        and not names & {lab["rework"], lab["failed"], settings()["hold"]}


def stack(prs, base):
    """A card's open pull requests bottom-up when they form one chain: the bottom one is based on `base` or on the branch of a
    merged pull request of the card, and each next one on the head of the one before. None for one pull request
    on `base` or pull requests that do not chain."""
    live = [p for p in prs if p.get("state") != "MERGED"]
    at = [base, *(p.get("headRefName") for p in prs if p.get("state") == "MERGED")]
    chain = []
    while len(starts := [p for p in live if p["baseRefName"] in at and p not in chain]) == 1 and len(chain) < len(live):
        chain.append(starts[0])
        at = [starts[0].get("headRefName")]
    if len(chain) != len(live) or not chain or (len(chain) == 1 and chain[0]["baseRefName"] == base):
        return None
    return chain


def candidates(all_cards, base=None):
    """[(card, pr)] of the open, non-draft pull requests of cards in PR open that carry ready and none of rework, failed or hold.
    The pull requests of a stack are candidates only all together, bottom-up, each tagged with its `stack`."""
    repo = C.TRACKER.get("repo")
    cards = sorted((c for c in all_cards if col_name(c.get("list_id")) == "PR open"),
                   key=lambda c: (agents.slice_order(c), (c.get("metadata") or {}).get("submitted_at") or c.get("updated_at") or ""))
    out = []
    for c in cards:
        prs = [_pr(r, num) for r, num, _ in rework._prs(c) if r == repo]
        if len(prs) > 1:
            base = base or merge_check.default_branch(repo)
        chain = stack(prs, base) if len(prs) > 1 else None
        if not chain:
            out += [(c, pr) for pr in prs if _eligible(pr)]
        elif all(_eligible(p) for p in chain):
            out += [(c, {**p, "stack": {"pos": k, "of": len(chain), "top": chain[-1], "below": chain[k - 2] if k > 1 else None}})
                    for k, p in enumerate(chain, 1)]
    return out


def rule_size(pr):
    files = pr.get("files") or []
    lines = sum(f["additions"] + f["deletions"] for f in files if not TEST_PATH.search(f["path"]))
    max_lines, max_files = C.PR_LIMITS["pr_auto_merge_lines"], C.PR_LIMITS["pr_auto_merge_files"]
    return {"ok": lines <= max_lines and len(files) <= max_files,
            "why": f"{lines} lines outside tests (limit {max_lines}), {len(files)} files (limit {max_files})"}


def rule_sensitive(card, pr, globs):
    if HUMAN_TAG in sections(card.get("description")).get("PLAN", "") or HUMAN_TAG in (pr.get("body") or ""):
        return {"ok": False, "why": f"tagged {HUMAN_TAG}"}
    hits = [f["path"] for f in pr.get("files") or [] if _match(f["path"], globs)]
    return {"ok": not hits, "why": ", ".join(hits[:3]) + (" and more" if len(hits) > 3 else "") if hits else "no sensitive tag or path"}


def rule_deps(card, all_cards):
    waiting = agents.deps_open(card, all_cards)
    kind, plan, num = agents.slice_order(card)
    for c in all_cards:
        k, p, n = agents.slice_order(c)
        if k == kind == 0 and p == plan and n < num \
                and (col_name(c.get("list_id")) != "Done" or (c.get("metadata") or {}).get("dropped_at")):
            waiting.append(c["id"])
    return {"ok": not waiting, "why": f"waiting for {', '.join(map(short_id, waiting))}" if waiting else "all dependencies are merged"}


def _stack_check(top, base):
    """The merge check of a stack, run once per pass on its top with the full suite."""
    key = (top["url"], top["headRefOid"])
    if key not in _STACK_CHECKS:
        _STACK_CHECKS[key] = merge_check.check(top, base, full=True)
    return _STACK_CHECKS[key]


def _checks(pr, base):
    st = pr.get("stack")
    if not st:
        return merge_check.check(pr, base)
    rules = _stack_check(st["top"], base)
    if st["pos"] == st["of"]:
        return rules
    return [{**r, "why": f"stack top #{st['top']['number']}: {r['why']}"} for r in rules]


def judge(card, pr, all_cards, base, write=False):
    rules = [{"rule": "size", **rule_size(pr)}, {"rule": "sensitive", **rule_sensitive(card, pr, settings()["sensitive"])},
             {"rule": "dependencies", **rule_deps(card, all_cards)}, *_checks(pr, base)]
    rules += judgment.rules(card, pr, write, asked=all(r["ok"] is True for r in rules))
    failed = [r["rule"] for r in rules if r["ok"] is False]
    not_run = [r["rule"] for r in rules if r["ok"] is None]
    action = "would merge" if not failed and not not_run else \
        f"needs a person: {', '.join(failed)}" if failed else f"needs a person: {', '.join(not_run)} not run"
    return {"card": card["id"], "pr": pr["url"], "title": card.get("title"), "rules": rules, "action": action}


def _said(repo, num, head):
    bodies = github._gh(["api", "--paginate", f"repos/{repo}/issues/{num}/comments?per_page=100", "--jq", ".[].body"])
    return MARK.format(head) in bodies


def _ensure_label(repo, name):
    if (repo, name) in _LABELS:
        return
    try:
        github._gh(["api", f"repos/{repo}/labels/{urllib.parse.quote(name, safe='')}"])
    except SystemExit:
        github._gh(["label", "create", name, "--repo", repo, "--color", "D93F0B",
                    "--description", "added by pl: a person decides whether to merge"])
    _LABELS.add((repo, name))


def _note(failed, head):
    lines = "\n".join(f"- {r['rule']}: {r['why']}" for r in failed)
    return ("pl merge loop: this pull request is not merged automatically, because:\n" + lines + "\n\n"
            "A person should decide: merge it by hand, change it, or close it. "
            "pl comments again only when the pull request gets a new commit.\n\n" + MARK.format(head))


def act(pr, v, dry):
    """Label and comment on a pull request whose judgement `v` has a failed rule, once per head commit; take the label off
    when none fails. Sets and returns v["loop"], what was (or with dry, would be) done."""
    repo, num = rework.PR_URL.match(pr["url"]).groups()
    failed = [r for r in v["rules"] if r["ok"] is False]
    lab = settings()["needs_person"]
    has = lab in {x["name"] for x in pr.get("labels") or []}
    api = f"repos/{repo}/issues/{num}"
    if not failed:
        v["loop"] = f"remove {lab}" if has else None
        if has and not dry:
            github._gh(["api", "-X", "DELETE", f"{api}/labels/{urllib.parse.quote(lab, safe='')}"])
        return v["loop"]
    head = pr["headRefOid"]
    if _said(repo, num, head):
        v["loop"] = "already commented on this head"
        if not has and not dry:
            _ensure_label(repo, lab)
            github._gh(["api", "-X", "POST", f"{api}/labels", "-f", f"labels[]={lab}"])
        return v["loop"]
    v["loop"] = f"add {lab} and comment"
    if not dry:
        _ensure_label(repo, lab)
        if not has:
            github._gh(["api", "-X", "POST", f"{api}/labels", "-f", f"labels[]={lab}"])
        github._gh(["api", "-X", "POST", f"{api}/comments", "-f", f"body={_note(failed, head)}"])
    return v["loop"]


def passes(verdict):
    return all(r["ok"] is True for r in verdict["rules"])


def _closed(url):
    m = rework.PR_URL.match(url)
    return json.loads(github._gh(["pr", "view", m.group(2), "--repo", m.group(1), "--json", "state"]))["state"] in ("MERGED", "CLOSED")


def _pending_path():
    return C.STATE_DIR / "merge-pending.json"


def _pending():
    try:
        return set(json.loads(_pending_path().read_text()))
    except (OSError, ValueError):
        return set()


def _save_pending(urls):
    _pending_path().write_text(json.dumps(sorted(urls)))


def _finish(card, url, all_cards, dry):
    """Record the merge on the card, then move it to Done once every other pull request of the card is closed. A failed
    move is left for the next pass: the record says the merge already happened."""
    from pl import commands
    repo, num, _ = next(p for p in rework._prs(card) if p[2] == url)
    if not dry:
        board.update(card["id"], metadata={"auto_merged": {"note": f"merged by auto-merge: {repo}#{num}", "pr": url,
                                                           "method": method(), "at": now_iso()}})
        _save_pending(_pending() - {url})
    if all(_closed(u) for _, _, u in rework._prs(card) if u != url):
        try:
            if not dry:
                commands.done(card["id"], all_cards)
        except SystemExit as e:
            print(f"{short_id(card['id'])}: merged, but not moved to Done yet ({e}); the next pass retries")


def _retry_finished(all_cards, dry):
    """Finish every merged pull request whose record or move to Done did not happen: the card has the record, or the
    merge was noted in the pending file before the record was written."""
    pending = _pending()
    for c in all_cards:
        if col_name(c.get("list_id")) != "PR open":
            continue
        url = ((c.get("metadata") or {}).get("auto_merged") or {}).get("pr") or next((u for _, _, u in rework._prs(c) if u in pending), None)
        if url:
            _finish(c, url, all_cards, dry)


def _retarget(card, pr, repo, base, dry):
    """The pull request pointed at the default branch and read again, or None when GitHub refused (logged)."""
    n = pr["number"]
    if dry:
        return {**pr, "baseRefName": base}
    try:
        github._gh(["api", "-X", "PATCH", f"repos/{repo}/pulls/{n}", "-f", f"base={base}"])
    except (RateLimited, OverBudget):
        raise
    except SystemExit as e:
        print(f"#{n} not retargeted to {base}: {e}")
        events.emit("auto_merge_failed", card["id"], pr=n, repo=repo, message=str(e))
        return None
    return {**_pr(repo, n), "stack": pr["stack"]}


def run_pass(all_cards, dry):
    """Merge, in candidate order, up to max_per_pass pull requests that pass every rule; each is judged right before it
    is merged, so it is checked against the default branch as the earlier merges left it, and its labels and state are
    read again after the judging, which can take minutes. Returns the merged URLs."""
    _retry_finished(all_cards, dry)
    _STACK_CHECKS.clear()
    merged, repo = [], C.TRACKER["repo"]
    base = merge_check.default_branch(repo)
    for card, pr in candidates(all_cards, base):
        if len(merged) >= per_pass():
            break
        n = pr["number"]
        if not passes(judge(card, pr, all_cards, base, write=not dry)):
            continue
        if pr.get("stack") and pr["baseRefName"] != base:
            below = pr["stack"]["below"]
            if below and below["url"] not in merged:
                print(f"#{n} waits for #{below['number']} in its stack")
                continue
            pr = _retarget(card, pr, repo, base, dry)
            if pr is None:
                continue
        if pr["baseRefName"] != base:
            msg = f"#{n} targets {pr['baseRefName']}, not the default branch {base}: not merged"
            print(msg)
            events.emit("auto_merge_failed", card["id"], pr=n, repo=repo, message=msg)
            continue
        if not _eligible(_pr(repo, n)):
            print(f"#{n} changed while it was judged: not merged")
            continue
        print(f"merging {repo}#{n} ({method()})")
        if not dry:
            try:
                github._gh(["pr", "merge", str(n), "--repo", repo, f"--{method()}", "--match-head-commit", pr["headRefOid"]])
            except (RateLimited, OverBudget):
                raise
            except SystemExit as e:
                print(f"#{n} not merged: {e}")
                events.emit("auto_merge_failed", card["id"], pr=n, repo=repo, message=str(e))
                continue
            _save_pending(_pending() | {pr["url"]})
            events.emit("auto_merged", card["id"], pr=n, repo=repo, method=method())
            _finish(card, pr["url"], all_cards, dry)
        merged.append(pr["url"])
    return merged


def cmd_merges(a):
    if C.TRACKER.get("type") != "github-project" or not C.TRACKER.get("repo"):
        raise SystemExit("pl merges: needs a github-project tracker with a repo")
    all_cards = board.cards()
    _STACK_CHECKS.clear()
    base = merge_check.default_branch(C.TRACKER["repo"])
    found = candidates(all_cards, base)
    judged = []
    for c, pr in found:
        v = judge(c, pr, all_cards, base, write=not a.dry_run)
        act(pr, v, a.dry_run)
        judged.append(v)
    rank = 0
    for j in judged:
        if passes(j):
            rank += 1
            j["action"] = "would merge this pass" if rank <= per_pass() else f"passes; waits for the next pass (max_per_pass = {per_pass()})"
    if a.json:
        return jsonout.emit({"candidates": judged})
    if not judged:
        return print("no pull requests are ready to merge")
    mark = {True: "ok", False: "FAIL", None: "—"}
    for j in judged:
        print(f"#{j['pr'].rsplit('/', 1)[-1]} {j['title']}")
        for r in j["rules"]:
            print(f"  {mark[r['ok']]}  {r['rule']}: {r['why']}")
        print(f"  -> {j['action']}")
        if j["loop"]:
            would = "would " if a.dry_run and not j["loop"].startswith("already") else ""
            print(f"  -> loop: {would}{j['loop']}")
