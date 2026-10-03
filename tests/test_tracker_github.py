"""WP5: GitHub Project and GitHub Issues trackers keep the card contract; every gh call goes through _gh."""
import json
import re
import subprocess

import pytest

from pl import config as C
from pl import trackers
from pl.trackers import github

OWNER, REPO, NUM = "acme", "acme/app", 7
OPTIONS = [{"id": "opt-inbox", "name": "Inbox"}, {"id": "opt-spec", "name": "Spec ready"},
           {"id": "opt-pr", "name": "PR open"}]


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    saved = {k: v for k, v in vars(C).items() if k.isupper()}
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("PL_CONFIG_DIR", "PL_TMUX_SESSION"):
        monkeypatch.delenv(var, raising=False)
    C.load()
    trackers.reset()
    yield tmp_path
    for k, v in saved.items():
        setattr(C, k, v)
    trackers.reset()


def _flags(args):
    pos, fl, i = [], {}, 0
    while i < len(args):
        a = args[i]
        if a == "--force":
            fl.setdefault(a, []).append(True)
        elif a.startswith("--"):
            fl.setdefault(a, []).append(args[i + 1])
            i += 1
        else:
            pos.append(a)
        i += 1
    return pos, fl


class FakeGh:
    """A tiny in-memory GitHub answering in gh's JSON shapes; every argv is logged."""

    def __init__(self, options=OPTIONS, field="Status"):
        self.log, self.issues, self.items, self.labels = [], {}, {}, set()
        self.label_create_fails = set()     # names whose `gh label create` fails
        self.search_ignores_project = set()  # issue numbers the intake search returns even when on the project
        self.item_add_fails = {}            # issue number -> the error `gh project item-add` raises for it
        self.options = list(options)
        self.field = field

    def issue_json(self, n):
        i = self.issues[n]
        return {"number": n, "title": i["title"], "body": i["body"], "url": i["url"], "state": i["state"],
                "updatedAt": "2026-09-28T10:00:00Z",
                "labels": [{"name": x} for x in i["labels"]], "assignees": [{"login": x} for x in i["assignees"]]}

    def _field(self, query):
        """The stage field as the query names it: GitHub answers null for a field the project does not have."""
        name = re.search(r'field\(name:"([^"]+)"\)', query).group(1)
        return {"id": "PVTSSF_status", "options": list(self.options)} if name == self.field else None

    def _value(self, status):
        opt = {o["id"]: o["name"] for o in self.options}
        return {"name": opt[status], "optionId": status} if status in opt else None

    def _content(self, n):
        i = self.issues[n]
        return {"number": n, "title": i["title"], "body": i["body"], "url": i["url"], "updatedAt": "2026-09-28T10:00:00Z",
                "labels": {"nodes": [{"name": x} for x in i["labels"]]},
                "assignees": {"nodes": [{"login": x} for x in i["assignees"]]}}

    def project(self, query):
        nodes = [{"id": "PVTI_draft", "fieldValueByName": None, "content": {}}]   # a draft: no Issue fields
        nodes += [{"id": iid, "fieldValueByName": self._value(it["status"]), "content": self._content(it["number"])}
                  for iid, it in self.items.items()]
        return {"id": "PVT_1", "field": self._field(query), "items": {"pageInfo": {"hasNextPage": False}, "nodes": nodes}}

    def issue_node(self, n, query):
        if n not in self.issues:
            return None
        items = [{"id": iid, "fieldValueByName": self._value(it["status"]),
                  "project": {"id": "PVT_1", "number": NUM, "owner": {"login": OWNER}, "field": self._field(query)}}
                 for iid, it in self.items.items() if it["number"] == n]
        return {**self._content(n), "projectItems": {"nodes": items}}

    def __call__(self, args):
        self.log.append(list(args))
        pos, fl = _flags(args)
        cmd = tuple(pos[:2])
        if cmd == ("project", "view"):
            return json.dumps({"id": "PVT_1", "number": NUM})
        if cmd == ("project", "field-list"):
            return json.dumps({"fields": [{"id": "PVTF_title", "name": "Title", "type": "ProjectV2Field"},
                                          {"id": "PVTSSF_status", "name": self.field, "type": "ProjectV2SingleSelectField",
                                           "options": self.options}], "totalCount": 2})
        if cmd == ("project", "item-list"):
            opt = {o["id"]: o["name"] for o in self.options}
            items = [{"id": "PVTI_draft", "title": "a draft", "content": {"type": "DraftIssue", "title": "a draft", "body": ""}}]
            for iid, it in self.items.items():
                i = self.issues[it["number"]]
                key = self.field[:1].lower() + self.field[1:]
                items.append({"id": iid, "title": i["title"], key: opt.get(it["status"]), "labels": list(i["labels"]),
                              "assignees": list(i["assignees"]), "repository": f"https://github.com/{REPO}",
                              "content": {"type": "Issue", "number": it["number"], "title": i["title"],
                                          "body": i["body"], "url": i["url"], "repository": REPO}})
            return json.dumps({"items": items, "totalCount": len(items)})
        if cmd == ("project", "item-add"):
            n = int(fl["--url"][0].rsplit("/", 1)[1])
            if n in self.item_add_fails:
                raise self.item_add_fails[n]
            iid = f"PVTI_{n}"
            self.items[iid] = {"number": n, "status": None}
            return json.dumps({"id": iid})
        if cmd == ("project", "item-edit"):
            self.items[fl["--id"][0]]["status"] = fl["--single-select-option-id"][0]
            return "{}"
        if cmd == ("project", "item-delete"):
            self.items.pop(fl["--id"][0])
            return "{}"
        if cmd == ("issue", "create"):
            self._known(fl.get("--label", []))
            n = len(self.issues) + 1
            self.issues[n] = {"title": fl["--title"][0], "body": fl["--body"][0], "labels": list(fl.get("--label", [])),
                              "assignees": list(fl.get("--assignee", [])), "state": "OPEN",
                              "url": f"https://github.com/{REPO}/issues/{n}"}
            return self.issues[n]["url"] + "\n"
        if cmd == ("issue", "view"):
            return json.dumps(self.issue_json(int(pos[2])))
        if cmd == ("issue", "list") and "--search" in fl:   # the issue intake: `-project:owner/n` hides project issues
            on = {it["number"] for it in self.items.values()} if f"-project:{OWNER}/{NUM}" in fl["--search"][0] else set()
            return json.dumps([{k: v for k, v in self.issue_json(n).items() if k in fl["--json"][0].split(",")}
                               for n, i in self.issues.items() if i["state"] == "OPEN" and (n not in on or n in self.search_ignores_project)])
        if cmd == ("api", "graphql") and "repositoryOwner(" in args[-1]:   # pl's board read: one page of items
            return json.dumps({"data": {"repositoryOwner": {"projectV2": self.project(args[-1])}}})
        if cmd == ("api", "graphql") and "projectItems" in args[-1]:        # pl's card lookup by issue
            return json.dumps({"data": {a: {"issue": self.issue_node(int(n), args[-1])} for a, n in
                                        re.findall(r'(i\d+):repository\(owner:"[^"]+",name:"[^"]+"\)\{issue\(number:(\d+)\)', args[-1])}})
        if cmd == ("issue", "list"):
            return json.dumps([self.issue_json(n) for n, i in self.issues.items() if i["state"] == "OPEN"])
        if cmd == ("issue", "edit"):
            self._known(lab for x in fl.get("--add-label", []) for lab in x.split(","))
            i = self.issues[int(pos[2])]
            for k in ("--title", "--body"):
                if k in fl:
                    i[k[2:]] = fl[k][0]
            for x in fl.get("--remove-label", []):
                i["labels"] = [lab for lab in i["labels"] if lab not in x.split(",")]
            for x in fl.get("--add-label", []):
                i["labels"] += [lab for lab in x.split(",") if lab not in i["labels"]]
            i["assignees"] += fl.get("--add-assignee", [])
            return i["url"] + "\n"
        if cmd == ("issue", "close"):
            self.issues[int(pos[2])]["state"] = "CLOSED"
            return ""
        if cmd == ("label", "create"):
            if pos[2] in self.label_create_fails:
                raise SystemExit(f"pl: gh label create: HTTP 403: cannot create {pos[2]}")
            self.labels.add(pos[2])
            return ""
        if cmd == ("label", "list"):
            return json.dumps([{"name": x} for x in sorted(self.labels)])
        raise AssertionError(f"unexpected gh call: {args}")

    def _known(self, labels):
        """Like GitHub: one label the repo does not have fails the whole issue create or edit."""
        for lab in labels:
            if lab not in self.labels:
                raise SystemExit(f"pl: gh issue create: could not add label: '{lab}' not found")


@pytest.fixture
def gh(monkeypatch):
    fake = FakeGh()
    monkeypatch.setattr(github, "_gh", fake)
    return fake


def project(**kw):
    return github.GitHubProject({"type": "github-project", "owner": OWNER, "number": NUM, "repo": REPO, **kw})


def issues(**kw):
    return github.GitHubIssues({"type": "github-issues", "repo": REPO, **kw})


def test_registry_builds_both_github_types(gh, monkeypatch):
    monkeypatch.setattr(C, "TRACKER", {"type": "github-project", "owner": OWNER, "number": NUM, "repo": REPO})
    assert isinstance(trackers.get("tracker"), github.GitHubProject)
    monkeypatch.setattr(C, "TRACKER", {"type": "github-issues", "repo": REPO})
    assert isinstance(trackers.get("tracker"), github.GitHubIssues)


@pytest.mark.parametrize("make", [project, issues], ids=["project", "issues"])
def test_round_trip_keeps_the_card_contract(gh, make):
    t = make()
    meta = {"pipeline_mode": "auto", "profile": "acme2", "nested": {"a": [1, 2]}}
    made = t.create("Inbox", title="Add export", description="line one\n\nline two", tags=("frontend", "acme"),
                    metadata=meta)
    assert made["id"] == f"{REPO}#1"
    got = t.card(made["id"])
    assert (got["title"], got["description"], got["tags"], got["metadata"]) == \
        ("Add export", "line one\n\nline two", ["frontend", "acme"], meta)
    assert got["url"] == t.url(made["id"]) == f"https://github.com/{REPO}/issues/1"
    assert got["list_id"] == t.columns()["Inbox"]
    body = gh.issues[1]["body"]
    assert body.count("pl:meta") == 1
    assert body.splitlines()[-1].startswith("<!-- pl:meta {") and body.endswith(" -->")


def test_comment_markers_in_description_and_metadata_survive(gh):
    t = project()
    desc = "see <!-- a comment --> here\nand a bare --> arrow\n<!-- open"
    meta = {"note": "ends with --> and <!-- too"}
    cid = t.create("Inbox", title="x", description=desc, metadata=meta)["id"]
    got = t.card(cid)
    assert got["description"] == desc
    assert got["metadata"] == meta
    assert gh.issues[1]["body"].splitlines()[-1].count("-->") == 1


def test_update_merges_metadata_and_verifies(gh):
    t = project()
    cid = t.create("Inbox", title="x", description="d", metadata={"keep": 1, "replace": "old"})["id"]
    got = t.update(cid, metadata={"replace": "new", "added": True}, description="d2")
    assert got["metadata"] == {"keep": 1, "replace": "new", "added": True}
    assert t.card(cid)["description"] == "d2"
    assert gh.issues[1]["body"].count("pl:meta") == 1


def test_update_raises_when_metadata_did_not_persist(gh, monkeypatch):
    t = project()
    cid = t.create("Inbox", title="x", metadata={"a": 1})["id"]
    real = gh.__call__

    def lossy(args):
        if args[:2] == ["issue", "edit"] and "--body" in args:
            i = args.index("--body") + 1
            args = [*args[:i], args[i].replace('"lost":"v"', '"lost":"other"'), *args[i + 1:]]
        return real(args)

    monkeypatch.setattr(github, "_gh", lossy)
    with pytest.raises(SystemExit, match="metadata.lost did not persist"):
        t.update(cid, metadata={"lost": "v"})


def test_project_move_sets_status_option(gh):
    t = project()
    cid = t.create("Inbox", title="x")["id"]
    gh.log.clear()
    t.update(cid, column="PR open")
    edits = [a for a in gh.log if a[:2] == ["project", "item-edit"]]
    assert len(edits) == 1
    _, fl = _flags(edits[0])
    assert fl["--single-select-option-id"] == ["opt-pr"]
    assert fl["--field-id"] == ["PVTSSF_status"] and fl["--project-id"] == ["PVT_1"] and fl["--id"] == ["PVTI_1"]
    assert t.card(cid)["list_id"] == "opt-pr"


def test_project_move_to_a_missing_option_says_add_it(gh):
    t = project()
    cid = t.create("Inbox", title="x")["id"]
    with pytest.raises(SystemExit, match="add a 'Done' option to the Status field"):
        t.update(cid, column="Done")


def test_project_cards_skip_draft_items(gh):
    t = project()
    t.create("Spec ready", title="real")
    assert [c["title"] for c in t.cards()] == ["real"]


def test_project_delete_removes_item_and_closes_issue(gh):
    t = project()
    cid = t.create("Inbox", title="x")["id"]
    t.delete(cid)
    assert gh.items == {} and gh.issues[1]["state"] == "CLOSED"


def test_issues_move_leaves_one_stage_label(gh):
    t = issues()
    cid = t.create("Inbox", title="x", tags=["acme"])["id"]
    t.update(cid, column="Approved")
    t.update(cid, column="PR open")
    labels = gh.issues[1]["labels"]
    assert [x for x in labels if x.startswith("pl:")] == ["pl:PR open"]
    assert "acme" in labels
    assert {"pl:Inbox", "pl:Approved", "pl:PR open"} <= gh.labels  # created when missing
    got = t.card(cid)
    assert got["tags"] == ["acme"] and got["list_id"] == t.columns()["PR open"]


def test_project_test_lists_missing_status_options(gh):
    out = project().test()
    missing = [c for c in C.COLUMNS if c not in {o["name"] for o in OPTIONS}]
    assert missing and all(m in out for m in missing)
    assert "Inbox" not in out.split("missing", 1)[1]
    gh.options = [{"id": f"o{i}", "name": c} for i, c in enumerate(C.COLUMNS)]
    assert "missing" not in project().test()


def test_gh_runner_uses_argv_and_a_timeout(monkeypatch):
    seen = {}

    def run(argv, **kw):
        seen.update(argv=argv, **kw)
        return subprocess.CompletedProcess(argv, 1, "", "error: your authentication token is missing required scopes [project]")

    monkeypatch.setattr(github.subprocess, "run", run)
    with pytest.raises(SystemExit, match="run: gh auth refresh -s project"):
        github._gh(["project", "field-list", "7"])
    assert seen["argv"] == ["gh", "project", "field-list", "7"]
    assert seen["timeout"] == 60 and not seen.get("shell")


def test_bad_metadata_line_does_not_break_the_card_list(gh):
    t = issues()
    t.create("Inbox", title="good", metadata={"a": 1})
    for body in ("hello\n<!-- pl:meta {not json} -->",
                 'hello\n<!-- pl:meta {"a":1} --> <!-- pl:meta {"b":2} -->'):
        gh.issues[2] = {**gh.issues[1], "title": "bad", "body": body}
        got = {c["title"]: c for c in t.cards()}
        assert got["good"]["metadata"] == {"a": 1}
        assert got["bad"]["metadata"] == {} and got["bad"]["description"] == body


def test_status_field_with_another_name_maps(gh):
    gh.field = "Stage"
    t = project(status_field="Stage")
    cid = t.create("PR open", title="x")["id"]
    assert t.card(cid)["list_id"] == "opt-pr"
    assert t.cards()[0]["list_id"] == "opt-pr"


def test_trailing_whitespace_after_the_meta_line_keeps_metadata(gh):
    t = issues()
    cid = t.create("Inbox", title="x", description="d", metadata={"a": 1})["id"]
    gh.issues[1]["body"] = gh.issues[1]["body"].replace("\n", "\r\n") + "\r\n  \n"
    assert "d\r\n<!-- pl:meta" in gh.issues[1]["body"]
    got = t.card(cid)
    assert got["metadata"] == {"a": 1} and got["description"] == "d"
    t.update(cid, metadata={"b": 2})
    assert gh.issues[1]["body"].count("pl:meta") == 1
    assert t.card(cid)["metadata"] == {"a": 1, "b": 2}
    assert t.card(cid)["description"] == "d"


def test_project_cards_colliding_numbers_keep_their_own_repo_and_stamp(gh, monkeypatch):
    stamps = {"acme/app": "2026-01-01T00:00:00Z", "acme/other": "2026-02-02T00:00:00Z"}

    def node(repo):
        return {"id": f"PVTI_{repo}", "fieldValueByName": {"name": "Inbox", "optionId": "opt-inbox"},
                "content": {"number": 5, "title": "t", "body": "", "url": f"https://github.com/{repo}/issues/5",
                            "updatedAt": stamps[repo], "labels": {"nodes": []}, "assignees": {"nodes": []}}}

    real = gh.__call__

    def two_repos(args):
        if args[:2] == ["api", "graphql"] and "repositoryOwner(" in args[-1]:
            proj = {"id": "PVT_1", "field": {"id": "PVTSSF_status", "options": OPTIONS},
                    "items": {"pageInfo": {"hasNextPage": False}, "nodes": [node("acme/app"), node("acme/other")]}}
            return json.dumps({"data": {"repositoryOwner": {"projectV2": proj}}})
        return real(args)

    monkeypatch.setattr(github, "_gh", two_repos)
    got = {c["id"]: c["updated_at"] for c in project().cards()}
    assert got == {"acme/app#5": "2026-01-01T00:00:00+00:00", "acme/other#5": "2026-02-02T00:00:00+00:00"}


def test_a_board_read_is_one_graphql_query_with_updated_at_and_no_gh_item_list(gh):
    """gh project item-list cost about 1 point per item (44 for a 44-card board); pl's own read costs 2 per 100 and
    brings updatedAt, the stage field and the project id with it."""
    t = project()
    t.create("Inbox", title="a")
    t.create("Inbox", title="b")
    gh.log.clear()
    trackers.reset()
    github.forget()
    got = project().cards()
    assert {c["updated_at"] for c in got} == {"2026-09-28T10:00:00+00:00"}
    assert [c["list_id"] for c in got] == ["opt-inbox", "opt-inbox"]
    (q,) = [a[-1] for a in gh.log]                        # no field-list, no item-list, no stamp query
    assert "items(first:100" in q and "rateLimit{" in q and "fieldValues" not in q


def test_a_board_read_pages_through_a_board_over_100_items(gh, monkeypatch):
    pages = []
    real = gh.__call__

    def paged(args):
        if args[:2] == ["api", "graphql"] and "repositoryOwner(" in args[-1]:
            after = re.search(r'after:"([^"]+)"', args[-1])
            pages.append(after.group(1) if after else None)
            n0 = 0 if not after else 100
            nodes = [{"id": f"PVTI_{n}", "fieldValueByName": None,
                      "content": {"number": n, "title": "t", "body": "", "url": f"https://github.com/acme/app/issues/{n}",
                                  "updatedAt": "2026-09-28T10:00:00Z", "labels": {"nodes": []}, "assignees": {"nodes": []}}}
                     for n in range(n0 + 1, n0 + (101 if not after else 31))]
            proj = {"id": "PVT_1", "field": {"id": "PVTSSF_status", "options": OPTIONS},
                    "items": {"pageInfo": {"hasNextPage": not after, "endCursor": "c100"}, "nodes": nodes}}
            return json.dumps({"data": {"repositoryOwner": {"projectV2": proj}}})
        return real(args)

    monkeypatch.setattr(github, "_gh", paged)
    assert len(project().cards()) == 130 and pages == [None, "c100"]


def test_a_card_is_one_issue_lookup_not_the_board(gh):
    t = project()
    cid = t.create("Inbox", title="a", metadata={"k": 1})["id"]
    trackers.reset()
    github.forget()
    gh.log.clear()
    c = project().card(cid)
    assert c["metadata"] == {"k": 1} and c["list_id"] == "opt-inbox" and c["item_id"]
    (q,) = [a[-1] for a in gh.log]
    assert "projectItems" in q and "repositoryOwner(" not in q
    with pytest.raises(SystemExit, match="no card acme/app#99"):
        project().card("acme/app#99")
    with pytest.raises(SystemExit, match="no card not-an-id"):
        project().card("not-an-id")


def test_update_reads_and_verifies_one_card_never_the_board(gh):
    t = project()
    cid = t.create("Inbox", title="a")["id"]
    gh.log.clear()
    t.update(cid, metadata={"worker": {"stage": "spec"}})
    assert not [a for a in gh.log if "repositoryOwner(" in a[-1]]
    assert len([a for a in gh.log if "projectItems" in a[-1]]) == 2   # the merge's read and the check after the write
    assert t.card(cid)["metadata"] == {"worker": {"stage": "spec"}}


@pytest.mark.parametrize("make", [project, issues], ids=["project", "issues"])
def test_crlf_description_round_trips_and_updates(gh, make):
    t = make()
    desc = "line one\r\nline two\r\n\r\nline four"
    cid = t.create("Inbox", title="x", description=desc, metadata={"a": 1})["id"]
    assert t.card(cid)["description"] == desc
    t.update(cid, description=desc + "\r\nmore")
    assert t.card(cid)["description"] == desc + "\r\nmore"
    t.update(cid, metadata={"b": 2})
    assert t.card(cid)["description"] == desc + "\r\nmore"


def test_gh_runner_reports_timeout_and_missing_gh(monkeypatch):
    def timeout(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 60)

    monkeypatch.setattr(github.subprocess, "run", timeout)
    with pytest.raises(SystemExit, match="gh timed out after 60s: gh project item-list"):
        github._gh(["project", "item-list", "7"])

    def missing(argv, **kw):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(github.subprocess, "run", missing)
    with pytest.raises(SystemExit, match="gh is not installed: https://cli.github.com"):
        github._gh(["issue", "list"])



def _every_gh_call(monkeypatch):
    """Run every place pl starts gh (tracker, watch, pr_counts, pl intent) against a fake; the env each got."""
    import argparse
    import os
    from pl import commands, watch
    envs, before = [], dict(os.environ)

    def run(argv, **kw):
        if argv[0] == "gh":
            envs.append(kw.get("env"))
        return subprocess.CompletedProcess(argv, 0, '{"body": ""}' if argv[1:3] == ["pr", "view"] else "[]", "")
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(commands, "cards", lambda *a, **k: [])
    monkeypatch.setattr(C, "CODE_HOST", {"owner": None, "labels": {"ready": "r", "failed": "f"}})
    watch._pr_cache.update(at=0, counts=None)
    github._gh(["auth", "status"])
    watch._gh(["search", "prs"])
    watch.pr_counts()
    commands.cmd_intent(argparse.Namespace(pr="https://github.com/o/r/pull/1"))
    assert len(envs) == 5 and dict(os.environ) == before     # os.environ itself is never changed
    return envs


def test_every_gh_call_gets_the_profiles_gh_config_dir(monkeypatch, tmp_path):
    import os
    monkeypatch.setattr(C, "GH_CONFIG_DIR", tmp_path / "gh-work", raising=False)
    envs = _every_gh_call(monkeypatch)
    assert all(e is not None and e["GH_CONFIG_DIR"] == str(tmp_path / "gh-work") for e in envs), envs
    assert all(e.get("PATH") == os.environ.get("PATH") for e in envs)   # a copy of the environment, plus one key


def test_gh_calls_inherit_the_environment_when_gh_config_dir_is_unset(monkeypatch):
    monkeypatch.setattr(C, "GH_CONFIG_DIR", None, raising=False)
    assert _every_gh_call(monkeypatch) == [None] * 5


@pytest.mark.parametrize("name", ["", "  "])
def test_a_blank_status_field_is_an_error_not_status(gh, name):
    with pytest.raises(SystemExit, match="status_field"):
        project(status_field=name)
    assert project().status_field == "Status"          # no key at all keeps the old default


def test_field_list_asks_for_up_to_100_fields(gh, monkeypatch):
    project().columns()
    assert all("--limit" in a and a[a.index("--limit") + 1] == "100" for a in gh.log if a[:2] == ["project", "field-list"])
    assert any(a[:2] == ["project", "field-list"] for a in gh.log)
    seen = []
    monkeypatch.setattr(github, "_gh", lambda args: seen.append(args) or '{"fields": []}')
    github.stage_field(OWNER, NUM, "pl stage")
    assert seen[0][seen[0].index("--limit") + 1] == "100"


def test_create_stage_field_refuses_a_column_with_a_comma(monkeypatch):
    seen = []
    monkeypatch.setattr(github, "_gh", lambda args: seen.append(args) or "")
    with pytest.raises(SystemExit, match="comma"):
        github.create_stage_field(OWNER, NUM, "pl stage", ["Inbox", "Spec, ready"])
    assert seen == []


# ---------- WP19: GraphQL helpers for pl setup ----------

def test_graphql_literals_escape_every_string_and_refuse_bad_enums():
    lit = github._lit({"name": 'Needs "you"\n}){x', "layout": github.GqlEnum("BOARD_LAYOUT"), "n": 14,
                       "opts": [{"id": "o1"}], "ok": True})
    assert lit == '{name:"Needs \\"you\\"\\n}){x",layout:BOARD_LAYOUT,n:14,opts:[{id:"o1"}],ok:true}'
    with pytest.raises(SystemExit):
        github._lit(github.GqlEnum("BOARD){x"))


def test_project_graph_asks_as_a_user_then_as_an_organisation(monkeypatch):
    calls = []

    def fake(args):
        calls.append(args)
        if "query{user(" in args[3]:
            raise SystemExit("pl: gh api graphql: Could not resolve to a User")
        return json.dumps({"data": {"organization": {"projectV2": {
            "id": "PVT_1", "views": {"nodes": [{"id": "V1", "name": "View 1"}]},
            "fields": {"nodes": [{"id": "F", "name": "Title", "dataType": "TITLE"}, {}]}}}}})
    monkeypatch.setattr(github, "_gh", fake)
    p = github.project_graph("acme", 7)
    assert p == {"id": "PVT_1", "views": [{"id": "V1", "name": "View 1"}],
                 "fields": [{"id": "F", "name": "Title", "dataType": "TITLE"}]}
    assert [a[:3] for a in calls] == [["api", "graphql", "-f"]] * 2
    assert calls[0][3].startswith('query=query{user(login:"acme"){projectV2(number:7){id views(first:50)')
    assert calls[1][3].startswith('query=query{organization(login:"acme"){projectV2(number:7)')


def test_mutations_are_one_argv_each_with_the_input_as_literals(monkeypatch):
    calls = []
    monkeypatch.setattr(github, "_gh", lambda args: (calls.append(args), '{"data": {}}')[1])
    github.update_view("V1", name="Pipeline", layout="BOARD_LAYOUT")
    github.set_field_options("F2", [{"id": "o1", "name": "Inbox", "color": "BLUE", "description": ""},
                                    {"name": "Done", "color": "GRAY", "description": ""}])
    assert calls == [
        ["api", "graphql", "-f", 'query=mutation{updateProjectV2View(input:{viewId:"V1",name:"Pipeline",'
                                 'layout:BOARD_LAYOUT}){clientMutationId}}'],
        ["api", "graphql", "-f", 'query=mutation{updateProjectV2Field(input:{fieldId:"F2",singleSelectOptions:['
                                 '{id:"o1",name:"Inbox",color:BLUE,description:""},'
                                 '{name:"Done",color:GRAY,description:""}]}){clientMutationId}}']]


# ---------- WP21: tag labels are created when missing ----------

def _label_calls(gh, verb):
    return [a for a in gh.log if a[:2] == ["label", verb]]


@pytest.mark.parametrize("make", [project, issues], ids=["project", "issues"])
def test_a_missing_tag_label_is_created_before_the_issue(gh, make):
    t = make()
    cid = t.create("Inbox", title="x", tags=["frontend"])["id"]
    creates = [a for a in _label_calls(gh, "create") if a[2] == "frontend"]
    assert creates == [["label", "create", "frontend", "--repo", REPO, "--color", "EDEDED", "--description", "added by pl"]]
    assert gh.log.index(creates[0]) < next(i for i, a in enumerate(gh.log) if a[:2] == ["issue", "create"])
    assert "frontend" in gh.issues[1]["labels"] and t.card(cid)["tags"] == ["frontend"]


@pytest.mark.parametrize("make", [project, issues], ids=["project", "issues"])
def test_an_existing_label_is_not_created_and_labels_are_listed_once(gh, make):
    gh.labels.add("frontend")
    t = make()
    t.create("Inbox", title="x", tags=["frontend"])
    t.create("Inbox", title="y", tags=["frontend"])
    assert not [a for a in _label_calls(gh, "create") if a[2] == "frontend"]
    assert len(_label_calls(gh, "list")) == 1
    assert gh.issues[2]["labels"].count("frontend") == 1


@pytest.mark.parametrize("make", [project, issues], ids=["project", "issues"])
def test_a_label_that_cannot_be_created_is_left_off_with_a_warning(gh, make, capsys):
    gh.label_create_fails.add("frontend")
    made = make().create("Inbox", title="x", tags=["frontend", "api"])
    assert made["id"] == f"{REPO}#1" and made["tags"] == ["api"]
    assert "frontend" not in gh.issues[1]["labels"] and "api" in gh.issues[1]["labels"]
    err = capsys.readouterr().err
    assert "frontend" in err and "warning" in err and len([x for x in err.splitlines() if "frontend" in x]) == 1


@pytest.mark.parametrize("make", [project, issues], ids=["project", "issues"])
def test_bad_label_names_are_dropped_with_a_warning(gh, make, capsys):
    made = make().create("Inbox", title="x", tags=["", "  ", "-R", "--repo=evil/x", "a,b", "ok"])
    assert made["tags"] == ["ok"]
    sent = [x for a in gh.log if a[:2] in (["issue", "create"], ["label", "create"]) for x in a]
    assert not any(x in ("", "  ", "-R", "--repo=evil/x", "a,b") for x in sent)
    assert "ok" in gh.issues[1]["labels"]
    assert len([x for x in capsys.readouterr().err.splitlines() if "warning" in x]) == 5


@pytest.mark.parametrize("make", [project, issues], ids=["project", "issues"])
def test_update_creates_a_missing_label_before_the_edit(gh, make):
    t = make()
    cid = t.create("Inbox", title="x")["id"]
    got = t.update(cid, tags=["backend"])
    assert got["tags"] == ["backend"]
    create = next(i for i, a in enumerate(gh.log) if a[:3] == ["label", "create", "backend"])
    edit = next(i for i, a in enumerate(gh.log) if a[:2] == ["issue", "edit"] and "--add-label" in a)
    assert create < edit


def test_existing_labels_are_never_edited_or_deleted(gh):
    gh.labels.add("frontend")
    t = issues()
    cid = t.create("Inbox", title="x", tags=["frontend", "new"])["id"]
    t.update(cid, tags=["new"])
    assert not [a for a in gh.log if a[:2] in (["label", "edit"], ["label", "delete"])]
    assert not [a for a in _label_calls(gh, "create") if a[2] in ("frontend", "new") and "--force" in a]


# ---------- WP36: a github-project profile adopts its repo's issues as funnel ideas ----------

def _issue(gh, title, labels=(), assignees=()):
    n = len(gh.issues) + 1
    gh.issues[n] = {"title": title, "body": f"the ask for {title}", "labels": list(labels), "assignees": list(assignees),
                    "state": "OPEN", "url": f"https://github.com/{REPO}/issues/{n}"}
    return n


def _intake_calls(gh):
    return [a for a in gh.log if a[:2] == ["issue", "list"] and "--search" in a]


def _intake(gh, monkeypatch, login="me"):
    from pl import product
    monkeypatch.setattr(C, "TRACKER", {"type": "github-project", "owner": OWNER, "number": NUM, "repo": REPO})
    monkeypatch.setattr(C, "INTAKE", {"columns": ["Triage"], "skip_tags": ["hold"]})
    monkeypatch.setattr(C, "PRODUCT_SKIP_TAGS", {"hold"})
    monkeypatch.setattr(C, "USER_LOGIN", login)
    monkeypatch.setattr(C, "ISSUE_INTAKE", {"repo": REPO, "start_label": "pl:start"})
    monkeypatch.setattr(product, "notify", lambda *a: None)
    monkeypatch.setattr(product, "healthy_profile", lambda *a: "main")
    gh.labels |= {"pl:start", "hold"}
    return product


SEARCH = f"-project:{OWNER}/{NUM} sort:updated-desc"


def test_issue_intake_adopts_assigned_and_start_labelled_issues_once(gh, monkeypatch):
    product = _intake(gh, monkeypatch)
    old = _issue(gh, "assigned before the first pass", assignees=["me"])
    start = _issue(gh, "labelled to start", labels=["pl:start"])
    _issue(gh, "someone else's", assignees=["other"])
    _issue(gh, "held", labels=["hold", "pl:start"], assignees=["me"])
    funnel = project().create("Inbox", title="already a funnel card", assigned_to="me")["id"]
    trackers.reset()

    assert [p["id"] for p in product.pull_new()] == [f"{REPO}#{start}"]   # first pass: labelled ones only, the rest seeded
    new = _issue(gh, "assigned after", assignees=["me"])
    trackers.reset()
    assert [p["id"] for p in product.pull_new()] == [f"{REPO}#{new}"]
    trackers.reset()
    assert product.pull_new() == []

    assert len(gh.issues) == 6                                          # adopted, never a second issue
    got = {c["id"]: c for c in project().cards()}
    for n in (start, new):
        c = got[f"{REPO}#{n}"]
        assert c["list_id"] == "opt-inbox" and c["metadata"]["pipeline_mode"] == "auto"
        assert "# PIPELINE: INPUT" in c["description"] and f"the ask for {gh.issues[n]['title']}" in c["description"]
        assert "me" in gh.issues[n]["assignees"]
    assert f"{REPO}#{old}" not in got and funnel in got
    calls = _intake_calls(gh)
    assert len(calls) == 3 and all(c[c.index("--limit") + 1] == "30" and
                                   c[c.index("--search") + 1] == SEARCH for c in calls)
    assert not [a for a in gh.log if a[:2] == ["label", "create"]]


def test_issue_intake_never_readopts_an_issue_already_on_the_project(gh, monkeypatch):
    product = _intake(gh, monkeypatch)
    n = _issue(gh, "on the board", labels=["pl:start"], assignees=["me"])
    gh.items["PVTI_x"] = {"number": n, "status": "opt-spec"}
    gh.search_ignores_project.add(n)          # GitHub's search index lags: the issue still shows up
    trackers.reset()
    assert product.pull_new() == [] and product.pull_new() == []
    assert gh.items["PVTI_x"]["status"] == "opt-spec"
    assert not [a for a in gh.log if a[:2] in (["issue", "edit"], ["project", "item-add"], ["project", "item-edit"])]


def test_issue_intake_removes_the_start_label_in_the_adopting_edit(gh, monkeypatch):
    product = _intake(gh, monkeypatch)
    start = _issue(gh, "labelled", labels=["pl:start", "bug"])
    gh.labels.add("bug")
    trackers.reset()
    product.pull_new()
    edits = [a for a in gh.log if a[:2] == ["issue", "edit"]]
    assert len(edits) == 1 and edits[0][edits[0].index("--remove-label") + 1] == "pl:start" and "--body" in edits[0]
    assert gh.issues[start]["labels"] == ["bug"]


def test_issue_intake_a_changed_login_is_a_new_first_pass(gh, monkeypatch):
    product = _intake(gh, monkeypatch, login="me")
    _issue(gh, "mine", assignees=["me"])
    theirs = _issue(gh, "theirs", assignees=["other"])
    trackers.reset()
    assert product.pull_new() == []                 # first pass as "me": remembered only
    monkeypatch.setattr(C, "USER_LOGIN", "other")
    trackers.reset()
    assert product.pull_new() == []                 # login changed: a new first pass, no flood of old assignments
    new = _issue(gh, "new for other", assignees=["other"])
    trackers.reset()
    assert [p["id"] for p in product.pull_new()] == [f"{REPO}#{new}"]
    assert f"{REPO}#{theirs}" not in {c["id"] for c in project().cards()}


def test_issue_intake_one_bad_issue_does_not_stop_the_pass(gh, monkeypatch, capsys):
    product = _intake(gh, monkeypatch)
    bad = _issue(gh, "breaks", labels=["pl:start"])
    good = _issue(gh, "works", labels=["pl:start"])
    _issue(gh, "mine before", assignees=["me"])
    gh.item_add_fails[bad] = SystemExit("pl: gh project item-add: HTTP 500")
    trackers.reset()
    assert [p["id"] for p in product.pull_new(quiet=True)] == [f"{REPO}#{good}"]
    assert f"{REPO}#{bad}" in capsys.readouterr().out
    assert C.SEEN_FILE.exists()
    limited = _issue(gh, "limited", labels=["pl:start"])
    gh.item_add_fails[limited] = github.RateLimited(0)
    trackers.reset()
    with pytest.raises(github.RateLimited):
        product.pull_new(quiet=True)


def test_issue_intake_quiet_pass_prints_no_skip_line(gh, monkeypatch, capsys):
    product = _intake(gh, monkeypatch)
    _issue(gh, "held", labels=["hold", "pl:start"])
    trackers.reset()
    product.pull_new(quiet=True)
    assert "skip" not in capsys.readouterr().out
    product.pull_new()
    assert "skip" in capsys.readouterr().out


@pytest.mark.parametrize("make", [project, issues])
def test_update_with_list_id_moves_the_card(gh, make):
    # pl approve / send back / done pass list_id=col_id(...), as the Acme board takes it
    t = make()
    cid = t.create("Spec ready", title="x", metadata={"keep": 1})["id"]
    got = t.update(cid, list_id=t.columns()["PR open"], metadata={"approved_at": "now", "worker": None})
    assert got["list_id"] == t.columns()["PR open"]
    assert t.card(cid)["list_id"] == t.columns()["PR open"]
    assert got["metadata"] == {"keep": 1, "approved_at": "now", "worker": None}


def test_ref_id_takes_the_short_repo_and_number_of_the_set_repo(gh):
    t = project()
    assert t.ref_id("app#12") == "acme/app#12" and t.ref_id("App#12") == "acme/app#12"
    assert t.ref_id("#12") == t.ref_id("12") == t.ref_id("acme/app#12") == "acme/app#12"
    assert t.ref_id("other/lib#3") == "other/lib#3"
    with pytest.raises(SystemExit) as e:
        t.ref_id("lib#3")   # another repo: only its full id says which owner
    assert "owner/lib#3" in str(e.value)
