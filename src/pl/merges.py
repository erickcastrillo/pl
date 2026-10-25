"""The merge judge: which open pull requests are ready to merge, in what order, and every merge rule's result.
The merge pass merges the ones that pass every rule; nothing here labels or comments on a pull request."""
import fnmatch
import json
import re

from pl import agents, board, events, jsonout, rework
from pl import config as C
from pl.board import col_name, sections
from pl.trackers import github
from pl.trackers.github import OverBudget, RateLimited
from pl.util import now_iso, short_id

SENSITIVE = ["**/migrations/**", "**/*.sql", "**/*.designer.cs", "**/schema*", "**/*auth*", "**/*token*", "**/*secret*", "**/*key*", ".github/**",
             "**/uv.lock", "**/package-lock.json", "**/yarn.lock", "**/poetry.lock", "**/Gemfile.lock", "**/go.sum", "**/Cargo.lock"]
HUMAN_TAG = "Requires Human Review"
TEST_PATH = re.compile(r"(^|/)tests?/|(^|/)test_[^/]*$|_test\.[^/]+$")
NOT_CHECKED = {"ok": None, "why": "not checked yet (later slice)"}


def settings():
    return {"hold": C.AUTO_MERGE.get("hold_label", "hold"), "sensitive": C.AUTO_MERGE.get("sensitive", SENSITIVE)}


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
                                  "number,url,state,isDraft,baseRefName,headRefOid,body,labels,files"]))


def candidates(all_cards):
    """[(card, pr)] of the open, non-draft pull requests of cards in PR open that carry ready and none of rework, failed or hold."""
    lab, hold, repo = rework.labels(), settings()["hold"], C.TRACKER.get("repo")
    cards = sorted((c for c in all_cards if col_name(c.get("list_id")) == "PR open"),
                   key=lambda c: (agents.slice_order(c), (c.get("metadata") or {}).get("submitted_at") or c.get("updated_at") or ""))
    out = []
    for c in cards:
        for r, num, _ in rework._prs(c):
            if r != repo:
                continue
            pr = _pr(r, num)
            names = {x["name"] for x in pr.get("labels") or []}
            if pr.get("state") == "OPEN" and not pr.get("isDraft") and lab["ready"] in names \
                    and not names & {lab["rework"], lab["failed"], hold}:
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


def judge(card, pr, all_cards):
    rules = {"size": rule_size(pr), "sensitive": rule_sensitive(card, pr, settings()["sensitive"]),
             "dependencies": rule_deps(card, all_cards), "tests": NOT_CHECKED, "conflicts": NOT_CHECKED}
    failed = [n for n, r in rules.items() if r["ok"] is False]
    return {"card": card["id"], "pr": pr["url"], "title": card.get("title"), "rules": [{"rule": n, **r} for n, r in rules.items()],
            "action": f"needs a person: {', '.join(failed)}" if failed else "would merge once tests and conflicts pass"}


def passes(verdict):
    return all(r["ok"] is True for r in verdict["rules"])


def default_branch(repo):
    return json.loads(github._gh(["api", f"repos/{repo}"]))["default_branch"]


def _closed(url):
    m = rework.PR_URL.match(url)
    return json.loads(github._gh(["pr", "view", m.group(2), "--repo", m.group(1), "--json", "state"]))["state"] in ("MERGED", "CLOSED")


def _finish(card, url, all_cards, dry):
    """Record the merge on the card, then move it to Done once every other pull request of the card is closed. A failed
    move is left for the next pass: the record says the merge already happened."""
    from pl import commands
    repo, num, _ = next(p for p in rework._prs(card) if p[2] == url)
    if not dry:
        board.update(card["id"], metadata={"auto_merged": {"note": f"merged by auto-merge: {repo}#{num}", "pr": url,
                                                           "method": method(), "at": now_iso()}})
    if all(_closed(u) for _, _, u in rework._prs(card) if u != url):
        try:
            if not dry:
                commands.done(card["id"], all_cards)
        except SystemExit as e:
            print(f"{short_id(card['id'])}: merged, but not moved to Done yet ({e}); the next pass retries")


def _retry_finished(all_cards, dry):
    for c in all_cards:
        if col_name(c.get("list_id")) == "PR open" and (am := (c.get("metadata") or {}).get("auto_merged")):
            _finish(c, am["pr"], all_cards, dry)


def run_pass(all_cards, dry):
    """Merge, in candidate order, up to max_per_pass pull requests that pass every rule; each is judged right before it
    is merged, so it is checked against the default branch as the earlier merges left it. Returns the merged URLs."""
    _retry_finished(all_cards, dry)
    merged, defaults = [], {}
    for card, pr in candidates(all_cards):
        if len(merged) >= per_pass():
            break
        if not passes(judge(card, pr, all_cards)):
            continue
        repo, n = C.TRACKER["repo"], pr["number"]
        base = defaults.setdefault(repo, default_branch(repo))
        if pr["baseRefName"] != base:
            msg = f"#{n} targets {pr['baseRefName']}, not the default branch {base}: not merged"
            print(msg)
            events.emit("auto_merge_failed", card["id"], pr=n, repo=repo, message=msg)
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
            events.emit("auto_merged", card["id"], pr=n, repo=repo, method=method())
            _finish(card, pr["url"], all_cards, dry)
        merged.append(pr["url"])
    return merged


def cmd_merges(a):
    if not a.dry_run:
        raise SystemExit("pl merges: only --dry-run exists so far")
    if C.TRACKER.get("type") != "github-project" or not C.TRACKER.get("repo"):
        raise SystemExit("pl merges: needs a github-project tracker with a repo")
    all_cards = board.cards()
    judged = [judge(c, pr, all_cards) for c, pr in candidates(all_cards)]
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
