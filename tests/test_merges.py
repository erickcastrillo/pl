"""The merge judge: which pull requests are candidates, in what order, and every rule's result; the merge loop labels
and comments on a failed rule once per head commit; the merge pass merges what passes."""
import json
import types
from datetime import datetime, timedelta
import urllib.parse

import pytest

from pl import accounts, agents, cli, commands, dispatch, merges
from pl import config as C

URL = "https://github.com/acme/app/pull/{}"


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PL_CONFIG_DIR", raising=False)
    d = tmp_path / ".pl-t"
    d.mkdir()
    (d / "config.toml").write_text('[tracker]\ntype = "github-project"\nowner = "acme"\nnumber = 3\nrepo = "acme/app"\n'
                                   '[code_host.labels]\nreview = "pl:auto-review"\nready = "pl:ready"\n')
    C.load("t")
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)


@pytest.fixture
def host(monkeypatch):
    """A fake code host and board. ns.prs: {number: pull request}; ns.cards: the board; ns.calls: every gh call;
    ns.comments: {number: [comment body]}; ns.repo_labels: the labels the repository already has."""
    ns = types.SimpleNamespace(prs={}, cards=[], calls=[], refuse={}, default="main", events=[], updates=[], done_fails=0, update_error=None,
                               comments={}, repo_labels=set(), check_calls=[], closing=False)

    def fake_gh(args):
        ns.calls.append(args)
        if args[:2] == ["pr", "merge"]:
            if msg := ns.refuse.get(int(args[2])):
                raise SystemExit(msg)
            if ns.closing and int(args[2]) in ns.prs:
                ns.prs[int(args[2])]["state"] = "MERGED"
            return ""
        if args[:3] == ["api", "-X", "PATCH"]:
            ns.prs[int(args[3].rsplit("/", 1)[1])]["baseRefName"] = args[5].removeprefix("base=")
            return ""
        if args[:2] == ["pr", "view"]:
            return json.dumps(ns.prs[int(args[2])])
        assert args[0] in ("api", "label"), args
        if args[:2] == ["label", "create"]:
            ns.repo_labels.add(args[2])
        elif args[0] == "api" and "--jq" in args:
            return "\n".join(ns.comments.get(int(args[2].split("/issues/")[1].split("/")[0]), []))
        elif args[0] == "api" and "/labels/" in args[1] and "-X" not in args:
            if urllib.parse.unquote(args[1].rsplit("/", 1)[1]) not in ns.repo_labels:
                raise SystemExit("gh: Not Found (HTTP 404)")
        return ""

    def fake_update(cid, **fields):
        if ns.update_error:
            raise ns.update_error
        ns.updates.append((cid, fields))
        next(c for c in ns.cards if c["id"] == cid)["metadata"].update(fields.get("metadata") or {})

    def fake_done(cid, cs=None):
        if ns.done_fails:
            ns.done_fails -= 1
            raise SystemExit("pl done: boom")
        next(c for c in ns.cards if c["id"] == cid)["list_id"] = "Done"
        return []
    monkeypatch.setattr(merges.board, "update", fake_update)
    monkeypatch.setattr(commands, "done", fake_done)
    monkeypatch.setattr(merges.events, "emit", lambda kind, cid=None, **d: ns.events.append((kind, cid, d)))
    monkeypatch.setattr(merges.github, "_gh", fake_gh)
    monkeypatch.setattr(merges, "_LABELS", set())
    monkeypatch.setattr(merges.board, "cards", lambda: ns.cards)
    ns.checks = [{"rule": "conflicts", "ok": True, "why": "merges cleanly onto main"}, {"rule": "tests", "ok": True, "why": "local tests passed; CI: none"}]
    monkeypatch.setattr(merges.merge_check, "default_branch", lambda repo: ns.default)
    monkeypatch.setattr(merges.merge_check, "check", lambda pr, base, **kw: (ns.check_calls.append((pr["number"], kw)), ns.checks)[1])
    monkeypatch.setattr(merges, "_STACK_CHECKS", {})
    monkeypatch.setattr(merges, "col_name", lambda lid: lid)
    monkeypatch.setattr(agents, "col_name", lambda lid: lid)
    return ns


def _pr(n, files=(("src/a.py", 10, 5),), labels=("pl:ready",), body="", state="OPEN", draft=False, head="abc"):
    return {"number": n, "url": URL.format(n), "state": state, "isDraft": draft, "baseRefName": "main", "body": body,
            "headRefOid": head,
            "labels": [{"name": x} for x in labels], "statusCheckRollup": [],
            "files": [{"path": p, "additions": a, "deletions": d} for p, a, d in files]}


def _card(cid, n=None, title=None, col="PR open", submitted="2026-10-01T00:00:00+00:00", desc="", **meta):
    n = n if n is not None else int(cid.removeprefix("c"))
    return {"id": cid, "title": title or f"card {cid}", "list_id": col, "tags": [], "description": desc,
            "metadata": {"pr_urls": [URL.format(n)], "submitted_at": submitted, **meta}}


def _add(host, card, pr):
    host.cards.append(card)
    host.prs[int(card["metadata"]["pr_urls"][0].rsplit("/", 1)[1])] = pr
    return card, pr


def _rules(host, card):
    j = merges.judge(card, host.prs[int(card["metadata"]["pr_urls"][0].rsplit("/", 1)[1])], host.cards, "main")
    return {r["rule"]: r for r in j["rules"]}, j


def test_only_ready_open_prs_without_rework_failed_or_hold_are_candidates(host):
    for n, kw in {1: {}, 2: {"labels": ("pl:ready", "pl:needs-rework")}, 3: {"labels": ("pl:ready", "pl:review-failed")},
                  4: {"labels": ("pl:ready", "hold")}, 5: {"labels": ("pl:auto-review",)}, 6: {"draft": True},
                  7: {"state": "MERGED"}}.items():
        _add(host, _card(f"c{n}"), _pr(n, **kw))
    _add(host, _card("c8", col="In progress"), _pr(8))
    assert [c["id"] for c, _ in merges.candidates(host.cards)] == ["c1"]


def test_candidates_order_earlier_slices_first_then_oldest_card(host):
    _add(host, _card("c1", submitted="2026-10-03T00:00:00+00:00"), _pr(1))
    _add(host, _card("c2", submitted="2026-10-01T00:00:00+00:00"), _pr(2))
    _add(host, _card("c3", title="Plan 5 slice 2: b"), _pr(3))
    _add(host, _card("c4", title="Plan 5 slice 1: a", submitted="2026-10-09T00:00:00+00:00"), _pr(4))
    assert [c["id"] for c, _ in merges.candidates(host.cards)] == ["c4", "c3", "c2", "c1"]


def test_small_clean_pr_passes_size_sensitive_and_deps(host):
    card, _ = _add(host, _card("c1"), _pr(1))
    rules, j = _rules(host, card)
    assert [rules[r]["ok"] for r in ("size", "sensitive", "dependencies")] == [True, True, True]
    assert rules["tests"]["ok"] is True and rules["conflicts"]["ok"] is True
    assert j["action"] == "would merge"


def test_too_big_fails_size_and_tests_lines_do_not_count(host):
    card, _ = _add(host, _card("c1"), _pr(1, files=(("src/a.py", 200, 12), ("tests/test_a.py", 900, 0), ("src/test_b.py", 500, 0))))
    rules, j = _rules(host, card)
    assert rules["size"]["ok"] is False
    assert rules["size"]["why"] == "212 lines outside tests (limit 150), 3 files (limit 6)"
    assert j["action"] == "needs a person: size"
    ok = _card("c2")
    _add(host, ok, _pr(2, files=(("src/a.py", 100, 0), ("tests/test_a.py", 900, 0))))
    assert _rules(host, ok)[0]["size"]["ok"] is True


def test_too_many_files_fails_size(host):
    card, _ = _add(host, _card("c1"), _pr(1, files=tuple((f"src/f{i}.py", 1, 0) for i in range(7))))
    rules, _ = _rules(host, card)
    assert rules["size"]["ok"] is False and "7 files (limit 6)" in rules["size"]["why"]


@pytest.mark.parametrize("path", ["db/migrations/0001_init.py", ".github/workflows/ci.yml", "uv.lock", "src/auth.py",
                                  "src/Data/Migrations/20260101_Init.cs", "Data/Migrations/Init.Designer.cs", "src/Models/Order.Designer.cs"])
def test_sensitive_path_fails(host, path):
    card, _ = _add(host, _card("c1"), _pr(1, files=((path, 1, 0),)))
    rules, j = _rules(host, card)
    assert rules["sensitive"]["ok"] is False and path in rules["sensitive"]["why"]
    assert j["action"] == "needs a person: sensitive"


def test_human_review_tag_on_plan_or_pr_body_fails(host):
    on_plan, on_body = _card("c1", desc="# PIPELINE: PLAN\nSensitivity: Security (Requires Human Review)"), _card("c2")
    _add(host, on_plan, _pr(1))
    _add(host, on_body, _pr(2, body="Requires Human Review"))
    for card in (on_plan, on_body):
        rules, _ = _rules(host, card)
        assert rules["sensitive"]["ok"] is False and "Requires Human Review" in rules["sensitive"]["why"]


def test_configured_sensitive_globs_replace_the_defaults(host):
    C.AUTO_MERGE = {"sensitive": ["docs/**"]}
    auth, docs = _card("c1"), _card("c2")
    _add(host, auth, _pr(1, files=(("src/auth.py", 1, 0),)))
    _add(host, docs, _pr(2, files=(("docs/x.md", 1, 0),)))
    assert _rules(host, auth)[0]["sensitive"]["ok"] is True
    assert _rules(host, docs)[0]["sensitive"]["ok"] is False


def test_unmerged_dependency_fails(host):
    _add(host, _card("c9", col="PR open"), _pr(9))
    card, _ = _add(host, _card("c1", depends_on=["c9"]), _pr(1))
    rules, j = _rules(host, card)
    assert rules["dependencies"]["ok"] is False and "c9" in rules["dependencies"]["why"]
    assert j["action"] == "needs a person: dependencies"


def test_earlier_slice_not_done_fails(host):
    host.cards.append(_card("c1", title="Plan 7 slice 1: a", col="PR open"))
    host.cards.append(_card("c2", title="Plan 7 slice 2: b", col="Done"))
    host.cards.append(_card("c3", title="Plan 8 slice 1: other plan", col="PR open"))
    later, _ = _add(host, _card("c4", title="Plan 7 slice 3: c"), _pr(4))
    assert _rules(host, later)[0]["dependencies"]["ok"] is False
    host.cards[0]["list_id"] = "Done"
    assert _rules(host, later)[0]["dependencies"]["ok"] is True
    host.cards[0]["metadata"]["dropped_at"] = "2026-10-02T00:00:00+00:00"
    assert _rules(host, later)[0]["dependencies"]["ok"] is False


def test_dry_run_prints_every_rule_and_writes_nothing(host, capsys):
    _add(host, _card("c1", title="Plan 1 slice 1: ok"), _pr(1))
    _add(host, _card("c2", title="Plan 1 slice 2: big"), _pr(2, files=(("src/a.py", 400, 0),)))
    merges.cmd_merges(types.SimpleNamespace(dry_run=True, json=False))
    out = capsys.readouterr().out
    assert "#1 Plan 1 slice 1: ok" in out and "  ok  size:" in out and "  FAIL  size: 400 lines outside tests" in out
    assert "  ok  conflicts: merges cleanly onto main" in out
    assert "  -> would merge" in out and "  -> needs a person: size" in out
    assert host.calls and not _writes(host)


def test_dry_run_json_is_one_document(host, capsys):
    _add(host, _card("c1"), _pr(1))
    merges.cmd_merges(types.SimpleNamespace(dry_run=True, json=True))
    doc = json.loads(capsys.readouterr().out)
    (cand,) = doc["candidates"]
    assert cand["card"] == "c1" and cand["pr"] == URL.format(1)
    assert [r["rule"] for r in cand["rules"]] == ["size", "sensitive", "dependencies", "conflicts", "tests", "judgment"]
    assert all(r["ok"] in (True, False, None) and r["why"] for r in cand["rules"])
    assert cand["action"] == "would merge this pass"


def test_conflict_reads_needs_a_person_conflicts(host, capsys):
    host.checks = [{"rule": "conflicts", "ok": False, "why": "conflicts with main: a.py"},
                   {"rule": "tests", "ok": None, "why": "not run: conflicts"}]
    _add(host, _card("c1"), _pr(1))
    merges.cmd_merges(types.SimpleNamespace(dry_run=True, json=False))
    out = capsys.readouterr().out
    assert "  FAIL  conflicts: conflicts with main: a.py" in out and "  —  tests: not run: conflicts" in out
    assert "  -> needs a person: conflicts" in out


def test_a_rule_that_did_not_run_is_never_would_merge(host):
    host.checks = [{"rule": "conflicts", "ok": None, "why": "not run: git fetch failed: x"},
                   {"rule": "tests", "ok": None, "why": "not run: git fetch failed"}]
    card, _ = _add(host, _card("c1"), _pr(1))
    assert _rules(host, card)[1]["action"] == "needs a person: conflicts, tests not run"


def test_dry_run_with_no_candidates_says_so(host, capsys):
    merges.cmd_merges(types.SimpleNamespace(dry_run=True, json=False))
    assert capsys.readouterr().out.strip() == "no pull requests are ready to merge"


def _run(dry_run=False, json_out=False):
    merges.cmd_merges(types.SimpleNamespace(dry_run=dry_run, json=json_out))


def _writes(host):
    return [c for c in host.calls if c[:2] == ["label", "create"] or "-X" in c]


def _comment_posts(host):
    return [c for c in host.calls if c[:2] == ["api", "-X"] and c[2] == "POST" and c[3].endswith("/comments")]


def _big(host, n=1, **kw):
    return _add(host, _card(f"c{n}"), _pr(n, files=(("src/a.py", 200, 12),), **kw))


def test_failed_rule_adds_label_creates_it_and_comments_once(host):
    _big(host)
    _run()
    assert ["label", "create", "needs-person", "--repo", "acme/app", "--color", "D93F0B",
            "--description", "added by pl: a person decides whether to merge"] in host.calls
    assert ["api", "-X", "POST", "repos/acme/app/issues/1/labels", "-f", "labels[]=needs-person"] in host.calls
    (post,) = _comment_posts(host)
    body = post[5].removeprefix("body=")
    assert body.startswith("pl merge loop: this pull request is not merged automatically, because:\n")
    assert "- size: 212 lines outside tests (limit 150), 1 files (limit 6)" in body
    assert "A person should decide" in body and body.endswith("<!-- pl:merge-loop head=abc -->")


def test_configured_needs_person_label_is_used(host):
    C.AUTO_MERGE = {"needs_person_label": "pl:merge-me"}
    _big(host)
    _run()
    assert ["api", "-X", "POST", "repos/acme/app/issues/1/labels", "-f", "labels[]=pl:merge-me"] in host.calls
    assert ["label", "create", "pl:merge-me"] == host.calls[[c[:2] for c in host.calls].index(["label", "create"])][:3]


def test_existing_label_is_not_created_again(host):
    host.repo_labels.add("needs-person")
    _big(host)
    _run()
    assert not [c for c in host.calls if c[:2] == ["label", "create"]]
    assert len(_comment_posts(host)) == 1


def test_same_head_already_commented_posts_nothing(host):
    host.comments[1] = ["a person's note", "pl merge loop: x\n\n<!-- pl:merge-loop head=abc -->"]
    _big(host, labels=("pl:ready", "needs-person"))
    _run()
    assert not _writes(host)


def test_same_head_already_commented_but_label_removed_puts_it_back_without_comment(host):
    host.comments[1] = ["<!-- pl:merge-loop head=abc -->"]
    host.repo_labels.add("needs-person")
    _big(host)
    _run()
    assert _writes(host) == [["api", "-X", "POST", "repos/acme/app/issues/1/labels", "-f", "labels[]=needs-person"]]


def test_comment_without_the_marker_does_not_count(host):
    host.comments[1] = ["pl merge loop: dry run said size", "<!-- pl:merge-loop head=def -->"]
    _big(host)
    _run()
    assert len(_comment_posts(host)) == 1


def test_new_head_still_failing_comments_again(host):
    host.comments[1] = ["<!-- pl:merge-loop head=abc -->"]
    _big(host, labels=("pl:ready", "needs-person"), head="def")
    _run()
    (post,) = _comment_posts(host)
    assert post[5].endswith("<!-- pl:merge-loop head=def -->")


def test_new_head_passing_removes_label_without_comment(host):
    _add(host, _card("c1"), _pr(1, labels=("pl:ready", "needs-person"), head="def"))
    _run()
    assert _writes(host) == [["api", "-X", "DELETE", "repos/acme/app/issues/1/labels/needs-person"]]


def test_unchecked_rules_do_not_count_as_failed(host):
    _add(host, _card("c1"), _pr(1))
    _run()
    assert merges.NOT_CHECKED["ok"] is None and not _writes(host) and not _comment_posts(host)


def test_passing_pr_without_label_gets_nothing(host):
    _add(host, _card("c1"), _pr(1))
    _run()
    assert not _writes(host) and not [c for c in host.calls if "--jq" in c]


def test_dry_run_says_would_and_writes_nothing(host, capsys):
    _big(host)
    _run(dry_run=True)
    assert "  -> loop: would add needs-person and comment" in capsys.readouterr().out
    host.comments[1] = ["<!-- pl:merge-loop head=abc -->"]
    _run(dry_run=True)
    assert "  -> loop: already commented on this head" in capsys.readouterr().out
    host.calls.clear()
    _run(dry_run=True, json_out=True)
    (cand,) = json.loads(capsys.readouterr().out)["candidates"]
    assert cand["loop"] == "already commented on this head"
    assert not _writes(host)


def test_dry_run_json_loop_is_null_for_a_passing_pr(host, capsys):
    _add(host, _card("c1"), _pr(1))
    _run(dry_run=True, json_out=True)
    assert json.loads(capsys.readouterr().out)["candidates"][0]["loop"] is None


def test_without_dry_run_merges_nothing(host):
    _big(host)
    _run()
    assert not [c for c in host.calls if c[:2] == ["pr", "merge"]]


def test_cli_routes_merges_without_dry_run(host, capsys, monkeypatch):
    _big(host)
    monkeypatch.setattr("sys.argv", ["pl", "--profile", "t", "merges"])
    cli.main()
    assert "  -> loop: add needs-person and comment" in capsys.readouterr().out


def test_cli_routes_merges_dry_run(host, capsys, monkeypatch):
    _add(host, _card("c1"), _pr(1))
    monkeypatch.setattr("sys.argv", ["pl", "--profile", "t", "merges", "--dry-run"])
    cli.main()
    out = capsys.readouterr().out
    assert "#1 card c1" in out and "  ok  size:" in out


# --- the merge pass ---

@pytest.fixture
def passing(host, monkeypatch):
    """Every candidate passes every rule, except the cards in host.failing (a callable may decide at judge time)."""
    host.failing = lambda cid: False
    host.needs = []
    host.on_judge = lambda cid: None

    def judge(card, pr, all_cards, base, write=False):
        host.calls.append(["judge", card["id"]])
        host.on_judge(card["id"])
        ok = not host.failing(card["id"])
        return {"card": card["id"], "pr": pr["url"], "title": card["title"], "rules": [{"rule": "size", "ok": ok, "why": ""}],
                "action": ""}
    monkeypatch.setattr(merges, "judge", judge)
    return host


def _merges_in(host):
    return [c for c in host.calls if c[:2] == ["pr", "merge"]]


def _ready(host, *ns):
    for n in ns:
        _add(host, _card(f"c{n}", submitted=f"2026-10-0{n}T00:00:00+00:00"), _pr(n))


def test_config_merge_method_defaults_and_allowed_values_pass():
    for tbl in ({}, {"merge_method": "squash"}, {"merge_method": "merge"}, {"merge_method": "rebase"}):
        assert not [e for e in C.validate({"loops": {"auto-merge": tbl}}) if "auto-merge" in e]


def test_config_bad_merge_method_names_key_and_values():
    (err,) = [e for e in C.validate({"loops": {"auto-merge": {"merge_method": "fast-forward"}}}) if "merge_method" in e]
    assert all(w in err for w in ("loops.auto-merge.merge_method", "squash", "merge", "rebase"))


@pytest.mark.parametrize("bad", [0, -1, 2.5, "3", True])
def test_config_bad_max_per_pass(bad):
    assert any("loops.auto-merge.max_per_pass" in e for e in C.validate({"loops": {"auto-merge": {"max_per_pass": bad}}}))


@pytest.mark.parametrize("ok", [1, 5])
def test_config_good_max_per_pass(ok):
    assert not any("max_per_pass" in e for e in C.validate({"loops": {"auto-merge": {"max_per_pass": ok}}}))


@pytest.mark.parametrize("method", ["squash", "merge", "rebase"])
def test_pass_merges_passing_candidate_with_configured_method(passing, method):
    C.AUTO_MERGE = {"enabled": True, "merge_method": method}
    _ready(passing, 123)
    assert merges.run_pass(passing.cards, dry=False) == [URL.format(123)]
    assert _merges_in(passing) == [["pr", "merge", "123", "--repo", "acme/app", f"--{method}", "--match-head-commit", "abc"]]


def test_merged_card_goes_to_done_with_note_and_one_event(passing):
    _ready(passing, 123)
    merges.run_pass(passing.cards, dry=False)
    (card,) = passing.cards
    assert card["list_id"] == "Done"
    assert card["metadata"]["auto_merged"]["note"] == "merged by auto-merge: acme/app#123"
    assert passing.events == [("auto_merged", "c123", {"pr": 123, "repo": "acme/app", "method": "squash"})]


def test_second_candidate_is_judged_after_first_merge(passing):
    _ready(passing, 1, 2)
    passing.failing = lambda cid: cid == "c2" and bool(_merges_in(passing))
    assert merges.run_pass(passing.cards, dry=False) == [URL.format(1)]
    order = [c[:2] if c[0] == "judge" else c[:3] for c in passing.calls if c[0] in ("judge", "pr") and c[1] != "view"]
    assert order == [["judge", "c1"], ["pr", "merge", "1"], ["judge", "c2"]]


def test_max_per_pass_caps_merges_in_candidate_order(passing):
    C.AUTO_MERGE = {"enabled": True, "max_per_pass": 2}
    _ready(passing, 1, 2, 3)
    assert merges.run_pass(passing.cards, dry=False) == [URL.format(1), URL.format(2)]
    assert ["judge", "c3"] not in passing.calls


def test_merge_refused_by_github_is_logged_and_pass_continues(passing):
    passing.refuse = {1: 'Required status check "ci" is failing'}
    _ready(passing, 1, 2)
    assert merges.run_pass(passing.cards, dry=False) == [URL.format(2)]
    a, b = passing.cards
    assert a["list_id"] == "PR open" and "auto_merged" not in a["metadata"] and b["list_id"] == "Done"
    (failed,) = [e for e in passing.events if e[0] == "auto_merge_failed"]
    assert failed[1] == "c1" and 'Required status check "ci" is failing' in failed[2]["message"]


def test_pr_not_on_default_branch_is_never_merged(passing):
    passing.default = "main"
    _add(passing, _card("c1"), {**_pr(1), "baseRefName": "release"})
    assert merges.run_pass(passing.cards, dry=False) == []
    assert _merges_in(passing) == []
    assert [e[0] for e in passing.events] == ["auto_merge_failed"]


def test_failing_rule_is_not_merged(passing):
    _ready(passing, 1)
    passing.failing = lambda cid: True
    assert merges.run_pass(passing.cards, dry=False) == []
    assert _merges_in(passing) == []


def test_unchecked_rule_never_passes():
    assert not merges.passes({"rules": [{"rule": "tests", "ok": None}]})
    assert merges.passes({"rules": [{"rule": "size", "ok": True}]})


def test_no_command_uses_admin_force_or_push(passing):
    passing.refuse = {2: "refused"}
    _ready(passing, 1, 2, 3, 4)
    passing.prs[3]["baseRefName"] = "release"
    passing.failing = lambda cid: cid == "c4"
    merges.run_pass(passing.cards, dry=False)
    assert _merges_in(passing)
    for argv in passing.calls:
        assert not {"--admin", "--auto", "--delete-branch", "--force", "--force-with-lease", "push"} & set(argv)
    assert all("--match-head-commit" in m for m in _merges_in(passing))


def test_done_move_failure_retries_next_pass_without_merging_again(passing):
    _ready(passing, 1)
    passing.done_fails = 1
    assert merges.run_pass(passing.cards, dry=False) == [URL.format(1)]
    (card,) = passing.cards
    assert card["list_id"] == "PR open" and "auto_merged" in card["metadata"]
    passing.prs[1]["state"] = "MERGED"
    merges.run_pass(passing.cards, dry=False)
    assert card["list_id"] == "Done" and len(_merges_in(passing)) == 1


def _labelled(host, n, name):
    host.prs[n]["labels"].append({"name": name})


@pytest.mark.parametrize("change", [
    lambda pr: pr["labels"].append({"name": "hold"}),
    lambda pr: pr["labels"].append({"name": "pl:needs-rework"}),
    lambda pr: pr["labels"].append({"name": "pl:review-failed"}),
    lambda pr: pr["labels"].clear(),
    lambda pr: pr.update(isDraft=True),
    lambda pr: pr.update(state="CLOSED"),
])
def test_pr_changed_after_listing_is_not_merged(passing, change):
    _ready(passing, 1, 2)
    passing.on_judge = lambda cid: change(passing.prs[1]) if cid == "c1" else None
    assert merges.run_pass(passing.cards, dry=False) == [URL.format(2)]
    assert [m[2] for m in _merges_in(passing)] == ["2"]


def test_merge_record_lost_to_a_budget_error_is_written_next_pass(passing):
    _ready(passing, 1)
    passing.update_error = merges.OverBudget(0, "low")
    with pytest.raises(merges.OverBudget):
        merges.run_pass(passing.cards, dry=False)
    (card,) = passing.cards
    assert len(_merges_in(passing)) == 1 and "auto_merged" not in card["metadata"]
    passing.update_error = None
    passing.prs[1]["state"] = "MERGED"
    assert merges.run_pass(passing.cards, dry=False) == []
    assert card["list_id"] == "Done" and card["metadata"]["auto_merged"]["pr"] == URL.format(1)
    assert len(_merges_in(passing)) == 1


def test_dry_pass_merges_nothing(passing):
    _ready(passing, 1)
    assert merges.run_pass(passing.cards, dry=True) == [URL.format(1)]
    assert _merges_in(passing) == [] and passing.events == [] and passing.updates == []


def test_dry_run_marks_this_pass_and_next_pass(passing, capsys):
    C.AUTO_MERGE = {"max_per_pass": 2}
    _ready(passing, 1, 2, 3)
    merges.cmd_merges(types.SimpleNamespace(dry_run=True, json=True))
    acts = [c["action"] for c in json.loads(capsys.readouterr().out)["candidates"]]
    assert acts == ["would merge this pass"] * 2 + ["passes; waits for the next pass (max_per_pass = 2)"]
    assert all(c[:2] == ["pr", "view"] or c[0] == "judge" for c in passing.calls)


# --- the dispatcher's call ---

@pytest.fixture
def due(monkeypatch):
    t = types.SimpleNamespace(now=datetime(2026, 10, 25, 12, 0, 0), runs=0, wait=None, parked=set(), result=None)
    monkeypatch.setattr(dispatch, "_local_now", lambda: t.now)
    monkeypatch.setattr(dispatch.ghquota, "check", lambda kind="read": t.wait)
    monkeypatch.setattr(accounts, "exhausted_profiles", lambda: t.parked)

    def run_pass(all_cards, dry):
        t.runs += 1
        if isinstance(t.result, BaseException):
            raise t.result
        return []
    monkeypatch.setattr(merges, "run_pass", run_pass)
    C.AUTO_MERGE = {"enabled": True, "every": "15m"}
    return t


def test_no_table_means_no_pass(due):
    C.AUTO_MERGE = {}
    st = {}
    dispatch._auto_merge(st, [], False)
    assert due.runs == 0 and "auto-merge" not in st.get("services", {})


def test_pass_runs_at_most_once_per_every(due):
    st = {}
    dispatch._auto_merge(st, [], False)
    assert due.runs == 1 and st["services"]["auto-merge"]["next_at"]
    due.now += timedelta(minutes=5)
    dispatch._auto_merge(st, [], False)
    assert due.runs == 1
    due.now += timedelta(minutes=11)
    dispatch._auto_merge(st, [], False)
    assert due.runs == 2


def test_low_budget_waits_and_says_so(due):
    due.wait = (due.now.timestamp() + 600, "spent")
    st = {}
    dispatch._auto_merge(st, [], False)
    assert due.runs == 0 and st["services"]["auto-merge"]["waits"].startswith("GitHub budget low (until ")


def test_parked_run_account_waits(due):
    C.STAGES = {"run": {"account": "alice"}}
    C.PROFILES = {"alice": {}}
    due.parked = {"alice"}
    st = {}
    dispatch._auto_merge(st, [], False)
    assert due.runs == 0 and st["services"]["auto-merge"]["waits"] == "its account alice is parked"


def test_failed_pass_is_recorded_and_next_due_set(due):
    due.result = SystemExit("pl: gh api: boom")
    st = {}
    dispatch._auto_merge(st, [], False)
    s = st["services"]["auto-merge"]
    assert s["last_run"]["result"].startswith("failed:") and s["next_at"]


# --- the Loops tab ---

def _row(monkeypatch, state):
    from pl.tui import loops
    C.TRACKER = {"type": "github-project", "repo": "acme/app"}
    monkeypatch.setattr(loops.util, "load_state", lambda: state)
    return next(r for r in loops.loop_rows({}) if r["name"] == "auto-merge")


def test_loops_tab_shows_auto_merge_off_without_table(monkeypatch):
    C.AUTO_MERGE = {}
    assert _row(monkeypatch, {})["state"] == "off"


def test_loops_tab_shows_last_and_next(monkeypatch):
    C.AUTO_MERGE = {"enabled": True}
    row = _row(monkeypatch, {"services": {"auto-merge": {"last_run": {"at": "2026-10-25T11:00:00+00:00", "result": "merged 2"},
                                                         "next_at": "2026-10-25T14:05:00"}}})
    assert row["state"] == "on" and row["next"] == "14:05" and row["output"] == "merged 2" and row["last"] != "-"


def test_loops_tab_shows_budget_and_parked_waits(monkeypatch):
    C.AUTO_MERGE = {"enabled": True}
    for why in ("GitHub budget low (until 14:05)", "its account alice is parked"):
        row = _row(monkeypatch, {"services": {"auto-merge": {"waits": why}}})
        assert row["state"] == "waiting" and row["output"] == f"waiting: {why}"


# --- stacks of pull requests ---

def _stack(host, cid, ns):
    """One card whose pull requests chain main -> b<ns[0]> -> b<ns[1]> ..."""
    card = {"id": cid, "title": f"card {cid}", "list_id": "PR open", "tags": [], "description": "",
            "metadata": {"pr_urls": [URL.format(n) for n in ns], "submitted_at": "2026-10-01T00:00:00+00:00"}}
    host.cards.append(card)
    for k, n in enumerate(ns):
        host.prs[n] = {**_pr(n), "baseRefName": "main" if k == 0 else f"b{ns[k - 1]}", "headRefName": f"b{n}"}
    return card


def _moves(host):
    return [("merge", int(c[2])) if c[:2] == ["pr", "merge"] else ("retarget", int(c[3].rsplit("/", 1)[1]))
            for c in host.calls if c[:2] == ["pr", "merge"] or c[:3] == ["api", "-X", "PATCH"]]


def test_stack_orders_bottom_up_from_the_default_branch(host):
    _stack(host, "c1", [1, 2, 3])
    shuffled = [host.prs[n] for n in (3, 1, 2)]
    assert [p["number"] for p in merges.stack(shuffled, "main")] == [1, 2, 3]
    assert merges.stack(shuffled[:1], "main") is None


def test_prs_that_do_not_chain_are_not_a_stack(passing):
    card = _stack(passing, "c1", [1, 2])
    passing.prs[2]["baseRefName"] = "elsewhere"
    assert merges.stack(list(passing.prs.values()), "main") is None
    assert merges.run_pass(passing.cards, dry=False) == [URL.format(1)]
    assert [e[0] for e in passing.events if e[0] == "auto_merge_failed"] == ["auto_merge_failed"]
    assert _moves(passing) == [("merge", 1)] and card["list_id"] == "PR open"


def test_stack_is_no_candidate_until_every_pr_is_ready(host):
    _stack(host, "c1", [1, 2, 3])
    assert [pr["number"] for _, pr in merges.candidates(host.cards)] == [1, 2, 3]
    host.prs[2]["labels"] = []
    assert merges.candidates(host.cards) == []


def test_stack_runs_the_merge_check_once_on_the_top_with_full(host):
    card = _stack(host, "c1", [1, 2, 3])
    verdicts = [merges.judge(card, pr, host.cards, "main") for _, pr in merges.candidates(host.cards)]
    assert host.check_calls == [(3, {"full": True})]
    assert all(v["action"] == "would merge" for v in verdicts)
    tests = [next(r for r in v["rules"] if r["rule"] == "tests")["why"] for v in verdicts]
    assert tests[0].startswith("stack top #3: ") and not tests[2].startswith("stack top")


def test_stack_merges_bottom_up_retargeting_each_to_the_default_branch(passing):
    _stack(passing, "c1", [1, 2, 3])
    assert merges.run_pass(passing.cards, dry=False) == [URL.format(n) for n in (1, 2, 3)]
    assert _moves(passing) == [("merge", 1), ("retarget", 2), ("merge", 2), ("retarget", 3), ("merge", 3)]
    assert all("--match-head-commit" in c for c in _merges_in(passing))
    assert [passing.prs[n]["baseRefName"] for n in (1, 2, 3)] == ["main"] * 3


def test_failing_stack_check_merges_none_of_the_stack(host):
    _stack(host, "c1", [1, 2, 3])
    host.checks = [{"rule": "conflicts", "ok": True, "why": "clean"}, {"rule": "tests", "ok": False, "why": "tests failed (exit 1)"}]
    assert merges.run_pass(host.cards, dry=False) == []
    assert _moves(host) == [] and host.check_calls == [(3, {"full": True})]


def test_stack_member_waits_when_the_one_below_was_not_merged_this_pass(passing, capsys):
    passing.refuse = {1: "refused"}
    _stack(passing, "c1", [1, 2])
    assert merges.run_pass(passing.cards, dry=False) == []
    assert _moves(passing) == [("merge", 1)] and "#2 waits for #1 in its stack" in capsys.readouterr().out


def test_max_per_pass_stops_a_stack_part_way_and_the_next_pass_goes_on(passing):
    C.AUTO_MERGE = {"enabled": True, "max_per_pass": 1}
    passing.closing = True
    _stack(passing, "c1", [1, 2])
    assert merges.run_pass(passing.cards, dry=False) == [URL.format(1)]
    assert _moves(passing) == [("merge", 1)]
    assert merges.run_pass(passing.cards, dry=False) == [URL.format(2)]
    assert _moves(passing) == [("merge", 1), ("retarget", 2), ("merge", 2)]


def test_card_moves_to_done_only_after_the_top_of_the_stack_merges(passing):
    C.AUTO_MERGE = {"enabled": True, "max_per_pass": 1}
    passing.closing = True
    card = _stack(passing, "c1", [1, 2, 3])
    for n in (1, 2):
        assert merges.run_pass(passing.cards, dry=False) == [URL.format(n)]
        assert card["list_id"] == "PR open"
    assert merges.run_pass(passing.cards, dry=False) == [URL.format(3)]
    assert card["list_id"] == "Done"
    assert _moves(passing) == [("merge", 1), ("retarget", 2), ("merge", 2), ("retarget", 3), ("merge", 3)]


def test_single_pr_card_is_checked_without_full_and_never_retargeted(host):
    _add(host, _card("c1"), _pr(1))
    assert merges.run_pass(host.cards, dry=False) == [URL.format(1)]
    assert host.check_calls == [(1, {})] and _moves(host) == [("merge", 1)]


def _tests_failed(host, ids=("tests/test_a.py::test_x", "tests/test_b.py::test_y")):
    host.checks = [{"rule": "conflicts", "ok": True, "why": "merges cleanly onto main"},
                   {"rule": "tests", "ok": False, "why": f"tests failed (exit 1): {', '.join(ids)}", "failing": list(ids)}]


def test_tests_only_failure_adds_rework_label_and_names_tests(host):
    _tests_failed(host)
    _add(host, _card("c1"), _pr(1))
    _run()
    assert ["api", "-X", "POST", "repos/acme/app/issues/1/labels", "-f", "labels[]=pl:needs-rework"] in host.calls
    assert not any("needs-person" in " ".join(c) for c in host.calls)
    (post,) = _comment_posts(host)
    body = post[5].removeprefix("body=")
    assert "- tests/test_a.py::test_x\n- tests/test_b.py::test_y" in body and body.endswith("<!-- pl:merge-loop head=abc -->")


def test_configured_rework_label_is_used(host):
    C.CODE_HOST["labels"]["rework"] = "x:rework"
    _tests_failed(host)
    _add(host, _card("c1"), _pr(1))
    _run()
    assert ["api", "-X", "POST", "repos/acme/app/issues/1/labels", "-f", "labels[]=x:rework"] in host.calls


def test_rework_comment_once_per_head(host):
    _tests_failed(host)
    _add(host, _card("c1"), _pr(1))
    host.comments[1] = ["<!-- pl:merge-loop head=abc -->"]
    _run()
    assert _comment_posts(host) == []


def test_tests_and_size_failures_still_need_a_person(host):
    _tests_failed(host)
    _add(host, _card("c1"), _pr(1, files=(("src/a.py", 200, 12),)))
    _run()
    assert ["api", "-X", "POST", "repos/acme/app/issues/1/labels", "-f", "labels[]=needs-person"] in host.calls
    (post,) = _comment_posts(host)
    assert "tests/test_a.py::test_x" in post[5]


def test_tests_failure_without_failed_lines_or_ci_needs_a_person(host):
    host.checks[1] = {"rule": "tests", "ok": False, "why": "timed out after 15 minutes"}
    _add(host, _card("c1"), _pr(1))
    _run()
    assert ["api", "-X", "POST", "repos/acme/app/issues/1/labels", "-f", "labels[]=needs-person"] in host.calls


def test_dry_run_says_would_add_rework_and_writes_nothing(host, capsys):
    _tests_failed(host)
    _add(host, _card("c1"), _pr(1))
    _run(dry_run=True)
    assert "would add pl:needs-rework and comment" in capsys.readouterr().out
    assert _writes(host) == []


def test_pass_labels_tests_only_failure_rework_and_merges_nothing(host):
    _tests_failed(host)
    _add(host, _card("c1"), _pr(1))
    assert merges.run_pass(host.cards, False) == []
    assert _merges_in(host) == []
    assert ["api", "-X", "POST", "repos/acme/app/issues/1/labels", "-f", "labels[]=pl:needs-rework"] in host.calls


def test_rework_label_from_merge_pass_is_flagged(host, monkeypatch):
    _tests_failed(host)
    _add(host, _card("c1"), _pr(1))
    merges.run_pass(host.cards, False)
    posted = [c for c in host.calls if c[:3] == ["api", "-X", "POST"] and c[3].endswith("/labels")]
    item = {"number": 1, "html_url": URL.format(1), "pull_request": {"url": "x"}, "labels": [{"name": c[5].removeprefix("labels[]=")} for c in posted]}
    monkeypatch.setattr(merges.rework.github, "_gh", lambda args: json.dumps([item]))
    assert merges.rework.flagged({"acme/app"}, merges.rework.labels()) == {("acme/app", 1)}
