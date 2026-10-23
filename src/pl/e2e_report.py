"""pl e2e report: read Playwright's JSON reporter output and write report.md / summary.json.

Takes plain data only. `run` is a dict the e2e command fills in:
  repo, ref, sha, vm {image, cpus, memory, disk}, started, finished (ISO), exit_code,
  outcome (passed | tests_failed | setup_failed | infra_failed),
  steps [{kind: setup|service|tests, name, status: ok|failed|timeout|skipped, seconds, log, tail}],
  artifacts_dir (where the VM's app folder was copied, relative to the report folder),
  vm_root (the app folder inside the VM, default /home/ubuntu/app).
Secret masking is the caller's job: log tails arrive already masked.
"""
import json
import posixpath
import re
from dataclasses import asdict, dataclass, field
from urllib.parse import quote

VM_ROOT = "/home/ubuntu/app"
ERROR_LINES = 15
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
TRACE_NOTE = ("Open a trace with `npx playwright show-trace <file>`, "
              "or drop it on https://trace.playwright.dev.")
VERDICT = {"passed": "PASSED", "tests_failed": "FAILED (tests)", "setup_failed": "FAILED (setup)",
           "infra_failed": "FAILED (infrastructure)"}


@dataclass
class Attachment:
    name: str
    content_type: str
    path: str


@dataclass
class Failure:
    title: str          # describe › describe › test
    project: str
    location: str       # file:line, the error's location when Playwright gave one, else the spec's
    error: str          # ANSI stripped, first ERROR_LINES lines
    attachments: list = field(default_factory=list)


@dataclass
class Results:
    passed: int = 0
    failed: int = 0
    flaky: int = 0
    skipped: int = 0
    duration: float = 0.0   # seconds
    failures: list = field(default_factory=list)
    flaky_titles: list = field(default_factory=list)
    errors: list = field(default_factory=list)   # errors outside any test (global setup, config)


def _clean(msg, limit=ERROR_LINES):
    lines = ANSI.sub("", str(msg or "")).rstrip().splitlines()
    return "\n".join(lines[:limit] + (["…"] if len(lines) > limit else []))


def _failure(spec, test, titles):
    results = test.get("results") or [{}]
    res = next((r for r in reversed(results) if r.get("status") not in ("passed", "skipped")), results[-1])
    err = res.get("error") or next(iter(res.get("errors") or []), None) or {}
    loc = err.get("location") or res.get("errorLocation") or {}
    where = f"{loc['file']}:{loc.get('line', 0)}" if loc.get("file") else f"{spec.get('file', '?')}:{spec.get('line', 0)}"
    atts = [Attachment(a.get("name", ""), a.get("contentType", ""), a["path"])
            for a in res.get("attachments") or [] if a.get("path")]
    return Failure(" › ".join(titles), test.get("projectName", ""), where, _clean(err.get("message")), atts)


def _walk(suite, titles, out, counts):
    for spec in suite.get("specs") or []:
        for test in spec.get("tests") or []:
            status = test.get("status")
            path = titles + [spec.get("title", "")]
            counts[status] = counts.get(status, 0) + 1
            if status == "unexpected":
                out.failures.append(_failure(spec, test, path))
            elif status == "flaky":
                out.flaky_titles.append(" › ".join(path))
    for child in suite.get("suites") or []:
        _walk(child, titles + [child.get("title", "")], out, counts)


def parse_playwright(path):
    """Results from Playwright's JSON report, or None when it is missing or not a report."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        out, counts = Results(), {}
        for suite in data.get("suites") or []:   # top level: one suite per file, its title is the file name
            _walk(suite, [], out, counts)
        stats = data.get("stats") or {}
        out.passed, out.failed, out.flaky, out.skipped = (
            int(stats.get(k, counts.get(k, 0))) for k in ("expected", "unexpected", "flaky", "skipped"))
        out.duration = round(float(stats.get("duration", 0)) / 1000, 4)
        out.errors = [_clean(e.get("message")) for e in data.get("errors") or []]
        return out
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        return None


def rebase_path(path, vm_root, host_root):
    """A path inside the VM's vm_root, as the same file under host_root; any other path comes back unchanged."""
    p, root = posixpath.normpath(path), posixpath.normpath(vm_root)
    if ".." in path.split("/") or not (p == root or p.startswith(root + "/")):
        return path
    rel = posixpath.relpath(p, root)
    return posixpath.normpath(posixpath.join(host_root, rel)) if host_root else rel


def _paths(run, results):
    """The failures with their file:line and attachments mapped from the VM to the report folder."""
    root, art = run.get("vm_root") or VM_ROOT, run.get("artifacts_dir") or "artifacts"
    out = []
    for f in results.failures if results else []:
        file, _, line = f.location.rpartition(":")
        out.append(Failure(f.title, f.project, f"{rebase_path(file, root, '')}:{line}", f.error,
                           [Attachment(a.name, a.content_type, rebase_path(a.path, root, art)) for a in f.attachments]))
    return out


def _verdict(run):
    outcome = run.get("outcome") or ("passed" if run.get("exit_code") == 0 else "tests_failed")
    return outcome, VERDICT.get(outcome, "FAILED")


def _cell(v):
    return str("" if v is None else v).replace("|", "\\|").replace("\n", " ")


def _link(text, path):
    return f"[{text}]({quote(path)})" if path else ""


def _fence(text):
    fence = "```"
    while fence in text:
        fence += "`"
    return f"{fence}\n{text}\n{fence}"


def _tests_ran(run):
    return any(s.get("kind") == "tests" and s.get("status") != "skipped" for s in run.get("steps") or [])


def render_markdown(run, results):
    outcome, verdict = _verdict(run)
    vm = run.get("vm") or {}
    lines = [f"# pl e2e: {verdict} — {run.get('repo', '?')}", "",
             f"- Repo: {run.get('repo', '?')}",
             f"- Ref: `{run.get('ref', '?')}` at `{str(run.get('sha') or '?')[:12]}`",
             "- VM: " + ", ".join(f"{k} {vm[k]}" for k in ("image", "cpus", "memory", "disk") if k in vm),
             f"- Started: {run.get('started', '?')}", f"- Finished: {run.get('finished', '?')}",
             f"- Exit code: {run.get('exit_code', '?')}", ""]
    steps = run.get("steps") or []
    if steps:
        lines += ["## Steps", "", "| Step | Kind | Status | Time | Log |", "|---|---|---|---|---|"]
        for s in steps:
            secs = s.get("seconds")
            took = f"{secs:.1f}s" if isinstance(secs, (int, float)) else ""
            lines.append("| " + " | ".join(_cell(x) for x in (
                s.get("name"), s.get("kind"), s.get("status"), took, _link(s.get("log"), s.get("log")))) + " |")
        lines.append("")
    for s in steps:
        if s.get("status") in ("failed", "timeout") and s.get("tail"):
            lines += [f"## Failed step: {s.get('name')} ({s.get('kind')}, {s.get('status')})", "",
                      f"Last lines of {_link(s.get('log'), s.get('log')) or 'its log'}:", "",
                      _fence("\n".join(s["tail"])), ""]
    lines += ["## Tests", ""]
    if results is None:
        lines.append("The tests did not run." if not _tests_ran(run) and outcome != "passed" else
                     f"The Playwright JSON report was missing or unreadable, so this verdict comes from "
                     f"the test command's exit code {run.get('exit_code', '?')}.")
        return "\n".join(lines) + "\n"
    lines.append(f"{results.passed} passed, {results.failed} failed, {results.flaky} flaky, "
                 f"{results.skipped} skipped in {results.duration:.1f}s")
    if results.flaky_titles:
        lines += ["", "Flaky (passed on retry):", *(f"- {t}" for t in results.flaky_titles)]
    for e in results.errors:
        lines += ["", "Error outside any test:", "", _fence(e)]
    failures = _paths(run, results)
    for f in failures:
        lines += ["", f"### {f.title}" + (f" [{f.project}]" if f.project else ""), "", f"`{f.location}`", "",
                  _fence(f.error or "(no error message)")]
        links = [_link(a.name or a.content_type, a.path) for a in f.attachments]
        if links:
            lines += ["", "Attachments: " + " · ".join(links)]
    if any(a.name == "trace" or a.path.endswith(".zip") for f in failures for a in f.attachments):
        lines += ["", TRACE_NOTE]
    return "\n".join(lines) + "\n"


def summary_json(run, results):
    outcome, _ = _verdict(run)
    tests = None if results is None else {k: getattr(results, k) for k in
                                          ("passed", "failed", "flaky", "skipped", "duration")}
    return {**{k: run.get(k) for k in ("repo", "ref", "sha", "vm", "started", "finished", "exit_code")},
            "outcome": outcome, "passed": outcome == "passed", "report_found": results is not None,
            "steps": run.get("steps") or [], "tests": tests,
            "failures": [asdict(f) for f in _paths(run, results)],
            "flaky": results.flaky_titles if results else [], "errors": results.errors if results else []}
