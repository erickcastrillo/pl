"""The merge judge: which pull requests are candidates, in what order, and every rule's result; the merge loop labels
and comments on a failed rule once per head commit. Nothing is merged."""
import json
import types
import urllib.parse

import pytest

from pl import agents, cli, merges
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
    ns = types.SimpleNamespace(prs={}, cards=[], calls=[], comments={}, repo_labels=set())

    def fake_gh(args):
        ns.calls.append(args)
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
    monkeypatch.setattr(merges.github, "_gh", fake_gh)
    monkeypatch.setattr(merges, "_LABELS", set())
    monkeypatch.setattr(merges.board, "cards", lambda: ns.cards)
    ns.checks = [{"rule": "conflicts", "ok": True, "why": "merges cleanly onto main"}, {"rule": "tests", "ok": True, "why": "local tests passed; CI: none"}]
    monkeypatch.setattr(merges.merge_check, "default_branch", lambda repo: "main")
    monkeypatch.setattr(merges.merge_check, "check", lambda pr, base: ns.checks)
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
    assert [r["rule"] for r in cand["rules"]] == ["size", "sensitive", "dependencies", "conflicts", "tests"]
    assert all(r["ok"] in (True, False, None) and r["why"] for r in cand["rules"])
    assert cand["action"] == "would merge"


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
