"""pl e2e report: Playwright JSON results into report.md and summary.json."""
import json

from pl.e2e_report import parse_playwright, rebase_path, render_markdown, summary_json

APP = "/home/ubuntu/app"
FE = f"{APP}/web/Frontend"
ESC = "\x1b"


def _res(status, duration=100, **kw):
    return {"workerIndex": 0, "status": status, "duration": duration, "errors": [], "attachments": [],
            "retry": 0, "startTime": "2026-10-22T10:00:00.000Z", **kw}


def _test(status, results, project="chromium"):
    return {"timeout": 30000, "annotations": [], "expectedStatus": "passed", "projectName": project,
            "projectId": project, "results": results, "status": status}


def _spec(title, tests, file="login.spec.ts", line=5):
    return {"tags": [], "title": title, "ok": all(t["status"] != "unexpected" for t in tests), "tests": tests,
            "id": title, "file": file, "line": line, "column": 1}


def _report(suites, stats=None, errors=()):
    return {"config": {}, "suites": suites, "errors": list(errors),
            "stats": stats or {"startTime": "2026-10-22T10:00:00.000Z", "duration": 4321.5,
                               "expected": 0, "unexpected": 0, "flaky": 0, "skipped": 0}}


def _write(tmp_path, data):
    p = tmp_path / "results.json"
    p.write_text(json.dumps(data) if not isinstance(data, str) else data)
    return p


FAIL_MSG = (f"Error: {ESC}[2mexpect({ESC}[22m{ESC}[31mlocator{ESC}[39m{ESC}[2m).toBeVisible(){ESC}[22m\n\n"
            "Locator: getByText('Welcome')\nExpected: visible\n" + "".join(f"line {i}\n" for i in range(30)))
FAIL_DIR = f"{FE}/test-results/login-signs in-chromium"
FAILED = _res("failed", 2500, error={"message": FAIL_MSG, "stack": "at x",
                                      "location": {"file": f"{FE}/e2e/login.spec.ts", "line": 12, "column": 7}},
              errors=[{"message": FAIL_MSG, "location": {"file": f"{FE}/e2e/login.spec.ts", "line": 12, "column": 7}}],
              attachments=[
                  {"name": "screenshot", "contentType": "image/png", "path": f"{FAIL_DIR}/test-failed-1.png"},
                  {"name": "video", "contentType": "video/webm", "path": f"{FAIL_DIR}/video.webm"},
                  {"name": "trace", "contentType": "application/zip", "path": f"{FAIL_DIR}/trace.zip"},
                  {"name": "stdout", "contentType": "text/plain", "body": "aGk="}])


def _mixed():
    """Nested describes, one failure, one flaky, one skipped, one pass."""
    return _report([{
        "title": "login.spec.ts", "file": "login.spec.ts", "line": 0, "column": 0, "specs": [
            _spec("opens the page", [_test("expected", [_res("passed")])], line=3)],
        "suites": [{"title": "Login", "file": "login.spec.ts", "line": 4, "column": 1, "specs": [
            _spec("is skipped", [_test("skipped", [_res("skipped", 0)])], line=20)],
            "suites": [{"title": "with a | pipe", "file": "login.spec.ts", "line": 6, "column": 3, "specs": [
                _spec("signs in", [_test("unexpected", [FAILED])], line=8),
                _spec("retries", [_test("flaky", [_res("failed"), _res("passed")])], line=30)]}]}]}],
        stats={"startTime": "x", "duration": 4321.5, "expected": 1, "unexpected": 1, "flaky": 1, "skipped": 1})


def _run(**kw):
    run = {"repo": "your-repo", "ref": "main", "sha": "0123456789abcdef0123", "vm_root": APP,
           "vm": {"image": "24.04", "cpus": 4, "memory": "8G", "disk": "30G"},
           "started": "2026-10-22T10:00:00+00:00", "finished": "2026-10-22T10:12:30+00:00",
           "steps": [{"kind": "setup", "name": "infra", "status": "ok", "seconds": 12.3,
                      "log": "logs/setup-1-infra.log", "tail": None},
                     {"kind": "service", "name": "api", "status": "ok", "seconds": 40,
                      "log": "logs/service-api.log", "tail": None},
                     {"kind": "tests", "name": "tests", "status": "failed", "seconds": 300,
                      "log": "logs/tests.log", "tail": ["1 failed"]}],
           "exit_code": 1, "outcome": "tests_failed", "artifacts_dir": "artifacts"}
    run.update(kw)
    return run


def test_all_passing(tmp_path):
    data = _report([{"title": "a.spec.ts", "file": "a.spec.ts", "line": 0, "column": 0, "specs": [
        _spec("one", [_test("expected", [_res("passed")])]), _spec("two", [_test("expected", [_res("passed")])])]}],
        stats={"startTime": "x", "duration": 1500, "expected": 2, "unexpected": 0, "flaky": 0, "skipped": 0})
    r = parse_playwright(_write(tmp_path, data))
    assert (r.passed, r.failed, r.flaky, r.skipped) == (2, 0, 0, 0)
    assert r.duration == 1.5 and r.failures == []
    md = render_markdown(_run(exit_code=0, outcome="passed", steps=[]), r)
    assert "PASSED" in md.splitlines()[0] and "FAILED" not in md and "2 passed" in md


def test_failure_with_attachments_and_ansi(tmp_path):
    r = parse_playwright(_write(tmp_path, _mixed()))
    assert (r.passed, r.failed, r.flaky, r.skipped) == (1, 1, 1, 1)
    [f] = r.failures
    assert f.title == "Login › with a | pipe › signs in" and f.project == "chromium"
    assert f.location == f"{FE}/e2e/login.spec.ts:12"
    assert ESC not in f.error and f.error.startswith("Error: expect(locator).toBeVisible()")
    assert len(f.error.splitlines()) == 16 and f.error.endswith("\n…")   # 15 lines, then a cut mark
    assert [a.name for a in f.attachments] == ["screenshot", "video", "trace"]   # inline bodies have no file
    assert f.attachments[2].content_type == "application/zip"


def test_flaky_and_skipped_are_not_failures(tmp_path):
    r = parse_playwright(_write(tmp_path, _mixed()))
    assert [f.title for f in r.failures] == ["Login › with a | pipe › signs in"]
    assert r.flaky_titles == ["Login › with a | pipe › retries"]


def test_counts_from_tests_when_stats_are_missing(tmp_path):
    data = _mixed()
    del data["stats"]
    r = parse_playwright(_write(tmp_path, data))
    assert (r.passed, r.failed, r.flaky, r.skipped) == (1, 1, 1, 1)


def test_location_falls_back_to_the_spec(tmp_path):
    data = _report([{"title": "b.spec.ts", "file": "b.spec.ts", "line": 0, "column": 0, "specs": [
        _spec("times out", [_test("unexpected", [_res("timedOut", error={"message": "Test timeout of 30000ms"})])],
              file="b.spec.ts", line=42)]}])
    [f] = parse_playwright(_write(tmp_path, data)).failures
    assert f.location == "b.spec.ts:42" and f.error == "Test timeout of 30000ms"


def test_top_level_errors_are_kept(tmp_path):
    r = parse_playwright(_write(tmp_path, _report([], errors=[{"message": f"{ESC}[31mglobalSetup failed{ESC}[39m"}])))
    assert r.errors == ["globalSetup failed"]
    assert "globalSetup failed" in render_markdown(_run(), r)


def test_missing_or_broken_json_gives_none(tmp_path):
    assert parse_playwright(tmp_path / "nope.json") is None
    assert parse_playwright(_write(tmp_path, "{not json")) is None
    assert parse_playwright(_write(tmp_path, "[1, 2]")) is None
    assert parse_playwright(_write(tmp_path, {"suites": "oops"})) is None


def test_rebase_path_inside_and_outside_the_vm_root():
    assert rebase_path(f"{APP}/web/x/trace.zip", APP, "artifacts") == "artifacts/web/x/trace.zip"
    assert rebase_path(f"{APP}/", APP, "artifacts") == "artifacts"
    assert rebase_path("/tmp/elsewhere/trace.zip", APP, "artifacts") == "/tmp/elsewhere/trace.zip"
    assert rebase_path(f"{APP}-other/a.png", APP, "artifacts") == f"{APP}-other/a.png"
    assert rebase_path(f"{APP}/../etc/passwd", APP, "artifacts") == f"{APP}/../etc/passwd"
    assert rebase_path(f"{APP}/web/e2e/a.ts", APP, "") == "web/e2e/a.ts"


def test_markdown_for_a_failed_run(tmp_path):
    md = render_markdown(_run(), parse_playwright(_write(tmp_path, _mixed())))
    assert "FAILED" in md.splitlines()[0]
    assert "your-repo" in md and "main" in md and "0123456789ab" in md and "8G" in md
    assert "1 passed" in md and "1 failed" in md and "1 flaky" in md and "1 skipped" in md
    assert "`web/Frontend/e2e/login.spec.ts:12`" in md
    assert "```\nError: expect(locator).toBeVisible()" in md
    base = "artifacts/web/Frontend/test-results/login-signs%20in-chromium"
    assert f"({base}/test-failed-1.png)" in md and f"({base}/video.webm)" in md and f"({base}/trace.zip)" in md
    assert "npx playwright show-trace" in md and "trace.playwright.dev" in md
    assert "[logs/tests.log](logs/tests.log)" in md


def test_pipes_are_escaped_in_tables():
    run = _run(steps=[{"kind": "setup", "name": "a|b", "status": "ok", "seconds": 1, "log": "logs/x.log", "tail": None}])
    md = render_markdown(run, None)
    assert "a\\|b" in md and "| a|b |" not in md


def test_setup_failure_shows_the_failing_step_and_its_tail():
    run = _run(outcome="setup_failed", exit_code=2, steps=[
        {"kind": "setup", "name": "infra", "status": "ok", "seconds": 3, "log": "logs/setup-1-infra.log", "tail": None},
        {"kind": "setup", "name": "dotnet", "status": "failed", "seconds": 9, "log": "logs/setup 2.log",
         "tail": ["downloading", "curl: (6) Could not resolve host"]},
        {"kind": "tests", "name": "tests", "status": "skipped", "seconds": 0, "log": None, "tail": None}])
    md = render_markdown(run, None)
    assert "FAILED" in md.splitlines()[0] and "setup" in md.splitlines()[0].lower()
    assert "dotnet" in md and "curl: (6) Could not resolve host" in md
    assert "(logs/setup%202.log)" in md
    assert "missing" not in md   # tests never ran: no complaint about their JSON


def test_missing_report_falls_back_to_the_exit_code():
    md = render_markdown(_run(exit_code=1), None)
    assert "FAILED" in md.splitlines()[0]
    assert "JSON report" in md and "missing" in md and "exit code 1" in md
    assert "PASSED" in render_markdown(_run(exit_code=0, outcome="passed"), None).splitlines()[0]


def test_summary_json(tmp_path):
    s = summary_json(_run(), parse_playwright(_write(tmp_path, _mixed())))
    json.dumps(s)
    assert s["outcome"] == "tests_failed" and s["exit_code"] == 1 and s["passed"] is False
    assert s["tests"] == {"passed": 1, "failed": 1, "flaky": 1, "skipped": 1, "duration": 4.3215}
    [f] = s["failures"]
    assert f["location"] == "web/Frontend/e2e/login.spec.ts:12"
    assert f["attachments"][2] == {"name": "trace", "content_type": "application/zip",
                                   "path": "artifacts/web/Frontend/test-results/login-signs in-chromium/trace.zip"}
    assert s["steps"][0]["name"] == "infra"
    missing = summary_json(_run(), None)
    assert missing["tests"] is None and missing["failures"] == [] and missing["report_found"] is False
