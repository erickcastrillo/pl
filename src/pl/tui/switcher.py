"""The ctrl+k switcher: jump to pipelines, tabs and cards across profiles."""
import json
import subprocess
import sys
import time

from rich.text import Text
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from pl import config as C
from pl.tui.cards import CardScreen
from pl.tui.chrome import tabs
from pl.tui.home import home_rows
from pl.util import short_id

_run = subprocess.run   # the one subprocess runner here; tests fake it
_CACHE = {}             # profile -> (timestamp, list_of_cards)


def is_subsequence(sub: str, full: str) -> bool:
    sub = sub.lower()
    full = full.lower()
    it = iter(full)
    return all(c in it for c in sub)


def item_text(item) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        parts = []
        for k in ("name", "title", "id", "card_id", "label", "text", "col", "profile", "tab_id"):
            if item.get(k):
                parts.append(str(item[k]))
        return " ".join(parts) if parts else str(item)
    if isinstance(item, (tuple, list)):
        return " ".join(str(x) for x in item)
    return str(item)


def matches(query, items):
    if not query:
        return list(items)
    return [item for item in items if is_subsequence(query, item_text(item))]


class SwitcherScreen(ModalScreen):
    """ctrl+k switcher over pipelines, tabs and cards."""
    DEFAULT_CSS = """
    SwitcherScreen { align: center middle; }
    SwitcherScreen > Vertical { width: 80; height: auto; max-height: 80%; border: round $accent; padding: 1 2; background: $surface; }
    SwitcherScreen OptionList { height: auto; max-height: 20; }
    """
    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self, data=None):
        super().__init__()
        self.data = data
        self.items = []
        self.filtered = []

    def compose(self):
        with Vertical():
            yield Input(placeholder="Switch to pipeline, tab, card…", id="switcher-input")
            yield OptionList(id="switcher-options")
            yield Static(Text("enter select · esc cancel", style="dim"))

    def on_mount(self):
        if self.data is None:
            self.data = getattr(self.app, "data", None) or {}
        self._build_initial_items()
        self._update_options()
        self.run_worker(self._fetch_remote, thread=True, group="switcher-remote")

    def _build_initial_items(self):
        items = []
        # Pipelines from data["home"]
        for p in (self.data.get("home") or []):
            name = p.get("name") if isinstance(p, dict) else str(p)
            items.append({
                "kind": "pipeline",
                "profile": name,
                "name": name,
                "label": f"Pipeline  {name}",
            })
        # Tabs from tabs()
        for tid, tname in tabs():
            items.append({
                "kind": "tab",
                "tab_id": tid,
                "name": tname,
                "label": f"Tab       {tname}",
            })
        # Cards from current profile
        rows = (self.data.get("snapshot") or {}).get("rows", [])
        for r in rows:
            c = r.get("card")
            if c:
                items.append({
                    "kind": "card",
                    "card_id": c["id"],
                    "profile": C.PROFILE_NAME,
                    "title": c.get("title", ""),
                    "col": r.get("col"),
                    "label": f"Card      {short_id(c['id'])}  {c.get('title', '')}",
                })
        self.items = items
        self.filtered = list(items)

    def _update_options(self):
        ol = self.query_one("#switcher-options", OptionList)
        ol.clear_options()
        ol.add_options([Option(item.get("label", item_text(item))) for item in self.filtered])
        if self.filtered:
            ol.highlighted = 0

    def on_input_changed(self, event):
        self.filtered = matches(event.value, self.items)
        self._update_options()

    def on_input_submitted(self, _):
        ol = self.query_one("#switcher-options", OptionList)
        idx = ol.highlighted if (ol.highlighted is not None and 0 <= ol.highlighted < len(self.filtered)) else 0
        if 0 <= idx < len(self.filtered):
            self._select_item(self.filtered[idx])

    def on_option_list_option_selected(self, event):
        if 0 <= event.option_index < len(self.filtered):
            self._select_item(self.filtered[event.option_index])

    def _select_item(self, item):
        self.dismiss(None)
        kind = item.get("kind")
        if kind == "tab":
            self.app.action_tab(item["tab_id"])
        elif kind == "card":
            if item.get("profile") and item["profile"] != C.PROFILE_NAME:
                self.app.switch_to(item["profile"], card=item["card_id"])
            else:
                self.app.push_screen(CardScreen(item["card_id"], item.get("col")))
        elif kind == "pipeline":
            self.app.switch_to(item["profile"])
        elif kind == "remote_card":
            self.app.switch_to(item["profile"], card=item["card_id"])

    def action_cancel(self):
        self.dismiss(None)

    def _add_remote_items(self, new_items):
        self.items.extend(new_items)
        inp = self.query_one("#switcher-input", Input)
        self.filtered = matches(inp.value, self.items)
        self._update_options()

    def _fetch_remote(self):
        home = (self.data or {}).get("home") or []
        now = time.time()
        remote_cards = []
        for p in home:
            if not isinstance(p, dict):
                continue
            name = p.get("name")
            if not name or name == C.PROFILE_NAME:
                continue
            if p.get("running") is False or p.get("state") == "stopped":
                continue
            cached = _CACHE.get(name)
            if cached and (now - cached[0] < 60):
                cards = cached[1]
            else:
                try:
                    r = _run([sys.executable, "-m", "pl", "--profile", name, "list", "--json"],
                             capture_output=True, text=True, timeout=10)
                    cards = json.loads(r.stdout) if r.returncode == 0 else []
                    if isinstance(cards, dict):
                        cards = cards.get("cards") or cards.get("rows") or []
                    _CACHE[name] = (now, cards)
                except Exception:
                    continue
            for c in cards:
                cid = c.get("id") or ""
                title = c.get("title") or ""
                remote_cards.append({
                    "kind": "remote_card",
                    "profile": name,
                    "card_id": cid,
                    "title": title,
                    "label": f"Card ({name})  {short_id(cid)}  {title}",
                })
        if remote_cards:
            self.app.call_from_thread(self._add_remote_items, remote_cards)


def picker_rows(data):
    summaries = [s for s in (data or {}).get("home") or [] if s and isinstance(s, dict)]
    return [{"name": r["name"], "type": s.get("pipeline") or "-", "running": r["running"],
             "live_agents": r["live_agents"], "needs_you": r["needs_you"], "current": r["name"] == C.PROFILE_NAME}
            for s, r in zip(summaries, home_rows(summaries))]


class PipelinesScreen(ModalScreen):
    """P: every pipeline on this machine; enter opens the chosen one on the tab you are on."""
    DEFAULT_CSS = """
    PipelinesScreen { align: center middle; }
    PipelinesScreen > Vertical { width: 80; height: auto; max-height: 80%; border: round $accent; padding: 1 2; background: $surface; }
    PipelinesScreen OptionList { height: auto; max-height: 20; }
    """
    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self, data=None):
        super().__init__()
        self.rows = picker_rows(data)

    def compose(self):
        with Vertical():
            yield OptionList(*(Option(self._line(i, r)) for i, r in enumerate(self.rows)), id="pipeline-options")
            yield Static(Text("↑↓ choose · enter switch · alt+1-9 jump (macOS: set Option as Meta) · esc close", style="dim"))

    @staticmethod
    def _line(i, r):
        key = f"alt+{i + 1}  " if i < 9 else " " * 7
        state = "running" if r["running"] else "stopped"
        return (f"{key}{'●' if r['current'] else ' '} {r['name']:<12} {r['type']:<14} {state:<8} "
                f"{r['live_agents']} working   {r['needs_you']} need you")

    def on_mount(self):
        ol = self.query_one(OptionList)
        ol.highlighted = next((i for i, r in enumerate(self.rows) if r["current"]), 0 if self.rows else None)
        ol.focus()

    def on_option_list_option_selected(self, event):
        self.dismiss(None)
        row = self.rows[event.option_index]
        if not row["current"]:
            self.app.choose_pipeline(row)

    def action_cancel(self):
        self.dismiss(None)
