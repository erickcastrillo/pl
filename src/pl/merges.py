"""The merge judge: which open pull requests are ready to merge, in what order, and every merge rule's result.
A failed rule gets the needs-person label and one comment per head commit. The merge pass merges the ones that pass every rule."""
import fnmatch
import json
import re
import urllib.parse

from pl import agents, board, events, jsonout, merge_check, rework
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
    return json.loads(github._gh(["pr", "view", str(num), "--repo", repo, "--json",
                                  "number,url,state,isDraft,baseRefName,headRefOid,body,labels,files,statusCheckRollup"]))


def _eligible(pr):
    lab, names = rework.labels(), {x["name"] for x in pr.get("labels") or []}
    return pr.get("state") == "OPEN" and not pr.get("isDraft") and lab["ready"] in names \
        and not names & {lab["rework"], lab["failed"], settings()["hold"]}


def candidates(all_cards):
    """[(card, pr)] of the open, non-draft pull requests of cards in PR open that carry ready and none of rework, failed or hold."""
    repo = C.TRACKER.get("repo")
    cards = sorted((c for c in all_cards if col_name(c.get("list_id")) == "PR open"),
                   key=lambda c: (agents.slice_order(c), (c.get("metadata") or {}).get("submitted_at") or c.get("updated_at") or ""))
    out = []
    for c in cards:
        for r, num, _ in rework._prs(c):
            if r != repo:
                continue
            pr = _pr(r, num)
            if _eligible(pr):
                out.append((c, pr))
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


def judge(card, pr, all_cards, base):
    rules = [{"rule": "size", **rule_size(pr)}, {"rule": "sensitive", **rule_sensitive(card, pr, settings()["sensitive"])},
             {"rule": "dependencies", **rule_deps(card, all_cards)}, *merge_check.check(pr, base)]
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


def run_pass(all_cards, dry):
    """Merge, in candidate order, up to max_per_pass pull requests that pass every rule; each is judged right before it
    is merged, so it is checked against the default branch as the earlier merges left it, and its labels and state are
    read again after the judging, which can take minutes. Returns the merged URLs."""
    _retry_finished(all_cards, dry)
    merged, defaults = [], {}
    for card, pr in candidates(all_cards):
        if len(merged) >= per_pass():
            break
        repo, n = C.TRACKER["repo"], pr["number"]
        base = defaults.setdefault(repo, merge_check.default_branch(repo))
        if not passes(judge(card, pr, all_cards, base)):
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
    found = candidates(all_cards)
    base = merge_check.default_branch(C.TRACKER["repo"]) if found else None
    judged = []
    for c, pr in found:
        v = judge(c, pr, all_cards, base)
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
