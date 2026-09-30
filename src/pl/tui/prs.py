"""Pull requests: the four pr_counts groups, an overview of the selected PR, and merge (m; M merges one not ready)."""
import json
import subprocess

from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import DataTable, Markdown, Static

from pl import config as C
from pl.commands import PR_URL, intent_text
from pl.setup import LABELS
from pl.trackers import github
from pl.tui.review import ConfirmScreen

_run = subprocess.run   # the one subprocess runner here (gh, argv only); tests fake it

GROUPS = [("decide", "NEED YOUR DECISION", "#e0a040"), ("merge", "READY TO MERGE", "green"),
          ("rework", "NEED REWORK", "red"), ("gate", "AWAITING MERGE CHECK", "dim")]
FIELDS = ("title,author,headRefName,baseRefName,additions,deletions,changedFiles,labels,statusCheckRollup,"
          "reviewDecision,mergeable,body")
METHODS = {"squash": ("--squash", "squash merge"), "merge": ("--merge", "merge commit")}
READY = "ready to merge"
BAD_URL = "not a GitHub pull request link"


def pr_groups(rows):
    g = {k: [] for k, *_ in GROUPS}
    for r in rows:
        if r.get("pr") and r["pr"]["state"] in g:
            g[r["pr"]["state"]].append(r["pr"])
    return g


def checks(rollup):
    """(passed, failed, pending) from gh's statusCheckRollup (check runs and commit statuses)."""
    passed = failed = pending = 0
    for c in rollup or []:
        s = str(c.get("conclusion") or c.get("state") or "").upper()
        if s in ("SUCCESS", "NEUTRAL", "SKIPPED"):
            passed += 1
        elif s in ("", "PENDING", "EXPECTED", "QUEUED", "IN_PROGRESS"):
            pending += 1
        else:
            failed += 1
    return passed, failed, pending


def _labels():
    return (C.CODE_HOST or {}).get("labels") or {}


def label_meaning(name):
    for key, (default, _, desc) in LABELS.items():
        if name in (default, _labels().get(key)):
            return f"{name} ({desc.lower()})"
    return name


def verdict(v):
    """READY when checks passed, GitHub says it merges cleanly, no reviewer asked for changes and it carries the
    merge-ready label (the profile's, when it renames it); else the first reason."""
    passed, failed, pending = checks(v.get("statusCheckRollup"))
    if failed:
        return f"{failed} check{'s' if failed != 1 else ''} failed"
    if pending:
        return f"{pending} check{'s' if pending != 1 else ''} still running"
    if v.get("mergeable") == "CONFLICTING":
        return "conflicts with the base branch"
    if v.get("mergeable") != "MERGEABLE":
        return "GitHub has not worked out yet whether it merges cleanly"
    if v.get("reviewDecision") == "CHANGES_REQUESTED":
        return "a reviewer asked for changes"
    if (_labels().get("merge_ready") or LABELS["merge_ready"][0]) not in {x.get("name") for x in v.get("labels") or []}:
        return "no merge-ready label: the merge check has not passed"
    return READY if passed else f"{READY} (no checks ran)"


def ready(why):
    return why.startswith(READY)


def fetch(url):
    """("ok", the gh pr view fields, the pl intent text) or ("error", note): one gh call, then the board read."""
    if not PR_URL.fullmatch(url or ""):
        return ("error", BAD_URL)
    if (lim := github.limited()) is not None:
        return ("error", lim.note)
    try:
        r = _run(["gh", "pr", "view", url, "--json", FIELDS], capture_output=True, text=True, timeout=60, env=C.gh_env())
    except (subprocess.TimeoutExpired, OSError) as e:
        return ("error", f"gh pr view failed: {e}")
    if r.returncode:
        lim = github.back_off(r.stderr)
        return ("error", lim.note if lim else (r.stderr or "gh pr view failed").strip()[:500])
    try:
        v = json.loads(r.stdout)
    except ValueError:
        return ("error", "gh pr view gave no JSON")
    try:
        intent = intent_text(url, v.get("body") or "")
    except (SystemExit, Exception) as e:  # noqa: BLE001 - a board error must not hide the overview
        intent = f"Could not read the card behind this PR: {getattr(e, 'note', None) or e}"
    return ("ok", v, intent)


def overview_text(pr, hit, method):
    t = Text()
    t.append(f"{pr.get('title') or ''}\n", style="bold")
    t.append(f"{pr['repo']}#{pr['number']}   {pr.get('url') or ''}\n\n", style="dim")
    if hit is None or hit[0] != "ok":
        t.append("loading…" if hit is None else hit[1], style="dim" if hit is None else "red")
        return t
    v = hit[1]
    passed, failed, pending = checks(v.get("statusCheckRollup"))
    labels = [label_meaning(x.get("name") or "") for x in v.get("labels") or []]
    for label, value in (("author", (v.get("author") or {}).get("login") or "-"),
                         ("branch", f"{v.get('headRefName') or '?'} into {v.get('baseRefName') or '?'}"),
                         ("size", f"+{v.get('additions', 0)} -{v.get('deletions', 0)}, {v.get('changedFiles', 0)} files"),
                         ("labels", ", ".join(labels) or "none"),
                         ("checks", f"{passed} passed, {failed} failed, {pending} pending"),
                         ("review", v.get("reviewDecision") or "none"),
                         ("mergeable", v.get("mergeable") or "unknown")):
        t.append(f"{label:<10}", style="dim")
        t.append(f"{value}\n")
    why = verdict(v)
    t.append("\n")
    t.append(why if ready(why) else f"not ready: {why}", style="bold green" if ready(why) else "bold red")
    t.append(f"\nm merge ({METHODS[method][1]})" + ("" if ready(why) else " · M merge anyway"), style="dim")
    return t


class MergeScreen(ConfirmScreen):
    BINDINGS = [Binding("enter", "answer(True)", "merge")]

    def compose(self):
        with Vertical():
            yield Static(Text(self.message))
            yield Static(Text("y / enter Merge · n no", style="dim"))


class PrsView(Horizontal):
    DEFAULT_CSS = """
    #prs-table { width: 84; height: 1fr; border: round $panel-lighten-2; }
    #prs-side { width: 1fr; height: 1fr; }
    #prs-body { height: 1fr; }
    """
    BINDINGS = [Binding("m", "merge(False)", "merge"), Binding("M", "merge(True)", "merge anyway")]

    def __init__(self):
        super().__init__()
        self._prs, self._built, self._shown = {}, [], None
        self._cache, self._pending, self._method = {}, set(), {}   # (url, updatedAt) → fetch(); repo → merge method
        self._err = None   # (ckey, fetch error) shown until the cursor moves or refresh; errors are never cached

    def compose(self):
        t = DataTable(id="prs-table", cursor_type="row", show_header=False, zebra_stripes=False)
        t.add_columns("pr")
        yield t
        with Vertical(id="prs-side", classes="panel"):
            d = Static(Text(""), id="prs-detail")
            d.styles.height = "auto"
            yield d
            with VerticalScroll(id="prs-body"):
                yield Markdown("", open_links=False, id="prs-md")

    def show(self, data):
        g = pr_groups(data["snapshot"]["rows"])
        built, self._prs = [], {}
        for key, title, colour in GROUPS:
            built.append((f"group:{key}", Text(f"■ {title} {len(g[key])}", style=f"bold {colour}")))
            for pr in g[key]:
                rid = pr.get("url") or f"{pr['repo']}#{pr['number']}"
                if rid in self._prs:   # a duplicate row would raise DuplicateKey
                    continue
                self._prs[rid] = pr
                row = Text(f"  {pr['repo']}#{pr['number']}  {pr['title'] or ''}")
                when = str(pr.get("updatedAt") or "")[:16].replace("T", " ")
                if when:
                    row.append(f"   updated {when} UTC", style="dim")
                built.append((rid, row))
            if not g[key]:
                built.append((f"none:{key}", Text("  none", style="dim")))
        t = self.query_one(DataTable)
        if [k for k, _ in built] == [k for k, _ in self._built]:
            col = next(iter(t.columns))
            for (rid, new), (_, was) in zip(built, self._built):
                if new != was:
                    t.update_cell(rid, col, new)
        else:
            keep = self._current()[0]
            t.clear()
            for rid, row in built:
                t.add_row(row, key=rid)
            first = next((i for i, (rid, _) in enumerate(built) if rid in self._prs), 0)
            t.move_cursor(row=t.get_row_index(keep) if keep in self._prs else first)
        self._built = built
        self.selected()

    def _current(self):
        t = self.query_one(DataTable)
        if not t.row_count:
            return None, None
        key = t.coordinate_to_cell_key((t.cursor_row, 0)).row_key.value
        return key, self._prs.get(key)

    @staticmethod
    def _ckey(pr):
        return pr.get("url"), str(pr.get("updatedAt") or "")

    def selected(self):
        """Paint the pane for the cursor row; read the PR from GitHub only while this tab is on screen."""
        _, pr = self._current()
        if pr is None or not pr.get("url"):
            self._paint(Text("select a pull request", style="dim"), "")
            return
        ck = self._ckey(pr)
        hit = self._cache.get(ck) or (self._err[1] if self._err and self._err[0] == ck else None)
        if hit is None and ck not in self._pending and self.app.active_tab == "prs":
            self._pending.add(ck)
            self.run_worker(lambda: self.app.call_from_thread(self._loaded, ck, fetch(pr["url"])),
                            thread=True, group="prs-view")
        self._paint(overview_text(pr, hit, self._method.get(pr["repo"], "squash")), hit[2] if hit and hit[0] == "ok" else "")

    def _loaded(self, ck, hit):
        self._pending.discard(ck)
        if hit[0] == "ok":
            self._cache[ck] = hit
        else:
            self._err = (ck, hit)
        _, pr = self._current()
        if pr is not None and self._ckey(pr) == ck:
            self.selected()

    def _paint(self, text, md):
        shown = (text.plain, md)
        if shown == self._shown:
            return
        md_changed = self._shown is None or self._shown[1] != md
        self._shown = shown
        self.query_one("#prs-detail", Static).update(text)
        if md_changed:
            self.query_one("#prs-md", Markdown).update(md)

    def clear_cache(self):
        self._cache.clear()
        self._err = None

    def action_merge(self, force):
        _, pr = self._current()
        if pr is None or not pr.get("url"):
            self.app.notify("select a pull request to merge")
            return
        if not PR_URL.fullmatch(pr["url"]):
            self.app.notify(f"not merging: {BAD_URL}", markup=False)
            return
        hit = self._cache.get(self._ckey(pr))
        v = hit[1] if hit and hit[0] == "ok" else None
        why = verdict(v) if v else "its overview has not loaded"
        name = f"{pr['repo']}#{pr['number']}"
        if not ready(why) and not force:
            self.app.notify(f"not merging {name}: {why}. Press M to merge it anyway.", markup=False)
            return
        method = self._method.get(pr["repo"], "squash")
        msg = (f"Merge {name}  {pr.get('title') or ''}\ninto {(v or {}).get('baseRefName') or 'its base branch'} "
               f"as a {METHODS[method][1]}?" + ("" if ready(why) else f"\n\nNot ready: {why}"))
        self.app.push_screen(MergeScreen(msg), lambda yes: yes and self._merge(pr, method, "m" if ready(why) else "M"))

    def _merge(self, pr, method, key="m"):
        """gh pr merge <url> --squash (or --merge) in a worker: no --admin, no --delete-branch."""
        def run():
            ok, repo, r = False, pr["repo"], None
            if (lim := github.limited()) is not None:
                note = lim.note
            else:
                try:
                    r = _run(["gh", "pr", "merge", pr["url"], METHODS[method][0]], capture_output=True, text=True,
                             timeout=120, env=C.gh_env())
                    out, ok = ((r.stdout or "") + (r.stderr or "")).strip(), r.returncode == 0
                except (subprocess.TimeoutExpired, OSError) as e:
                    out = str(e)
                low = out.lower()
                if ok:
                    note = out or f"merged {repo}#{pr['number']}"
                elif method == "squash" and "squash" in low and "not allowed" in low:
                    self._method[repo] = "merge"
                    note = f"{repo} does not allow squash merges: press {key} again to merge with a merge commit"
                else:
                    lim = github.back_off(r.stderr) if r is not None else None
                    note = lim.note if lim else f"merge failed: {out or 'gh gave no reason'}"
            self.app.call_from_thread(self.app.notify, note, severity="information" if ok else "error", markup=False)
            if ok:
                self._cache.pop(self._ckey(pr), None)
                self.app.call_from_thread(self.app.refresh_data)
            else:
                self.app.call_from_thread(self.selected)
        self.run_worker(run, thread=True, group="prs-merge")

    def on_data_table_row_highlighted(self, _):
        self._err = None
        self.selected()
