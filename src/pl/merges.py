"""The merge judge: which open pull requests are ready to merge, in what order, and every merge rule's result.
Nothing here merges, labels or comments on a pull request."""
import fnmatch
import json
import re

from pl import agents, board, jsonout, rework
from pl import config as C
from pl.board import col_name, sections
from pl.trackers import github
from pl.util import short_id

SENSITIVE = ["**/migrations/**", "**/*.sql", "**/schema*", "**/*auth*", "**/*token*", "**/*secret*", "**/*key*", ".github/**",
             "**/uv.lock", "**/package-lock.json", "**/yarn.lock", "**/poetry.lock", "**/Gemfile.lock", "**/go.sum", "**/Cargo.lock"]
HUMAN_TAG = "Requires Human Review"
TEST_PATH = re.compile(r"(^|/)tests?/|(^|/)test_[^/]*$|_test\.[^/]+$")
NOT_CHECKED = {"ok": None, "why": "not checked yet (later slice)"}


def settings():
    return {"hold": C.AUTO_MERGE.get("hold_label", "hold"), "sensitive": C.AUTO_MERGE.get("sensitive", SENSITIVE)}


def _match(path, globs):
    return any(fnmatch.fnmatch(path, g) or (g.startswith("**/") and fnmatch.fnmatch(path, g[3:])) for g in globs)


def _pr(repo, num):
    return json.loads(github._gh(["pr", "view", str(num), "--repo", repo, "--json",
                                  "number,url,state,isDraft,baseRefName,body,labels,files"]))


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


def cmd_merges(a):
    if not a.dry_run:
        raise SystemExit("pl merges: only --dry-run exists so far")
    if C.TRACKER.get("type") != "github-project" or not C.TRACKER.get("repo"):
        raise SystemExit("pl merges: needs a github-project tracker with a repo")
    all_cards = board.cards()
    judged = [judge(c, pr, all_cards) for c, pr in candidates(all_cards)]
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
