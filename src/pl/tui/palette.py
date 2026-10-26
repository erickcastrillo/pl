"""The console's actions as rows: what ? lists, and what Ctrl+P finds and runs."""
from textual.binding import ActiveBinding
from textual.app import SystemCommand
from textual.widgets import DataTable, TabPane

from pl.tui import parity
from pl.tui.chrome import GROUP_KEYS, MEANING, groups, tabs

SKIPPED = {"tab", "sub_tab", "sub_letter"}   # the tab rows already go to every tab


def rows(bindings):
    """(group, key, description, command, action, node) per enabled described binding, one per description."""
    found, seen = [], set()
    for key, active in bindings:
        b = active.binding
        if active.enabled and b.description and b.description not in seen:
            seen.add(b.description)
            command = parity.command_for(type(active.node).__name__, b.action, key)
            found.append((parity.group_of(b.action, command), b.key_display or key, b.description,
                          command or "console only", b.action, active.node))
    return found


def tab_bindings(app, tid, app_tabs):
    """The (key, ActiveBinding) pairs ? would show on tab tid, read from its focus chain without switching to it."""
    pane = app.query_one(f"#{tid}", TabPane)
    start = next((w for w in pane.query("*") if w.focusable), pane)
    found = {}
    for node in start.ancestors_with_self:
        for key, bindings in node._bindings.key_to_bindings.items():
            for b in bindings:
                if node is not app or app_tabs.get(b.action.partition("(")[0]) == tid:
                    found.setdefault(key, ActiveBinding(node, b, True, ""))
        if node is app.screen:
            break
    return found.items()


def _title(description, key, action):
    """The search matches titles only, so the title also carries the action's meaning and its name."""
    name = action.partition("(")[0].rpartition(".")[2]
    extra = [w for w in (MEANING.get(action) or MEANING.get(name), name.replace("_", " ")) if w]
    return " · ".join([f"{description} ({key})", *dict.fromkeys(w for w in extra if w not in description)])


def _tab_of(vid):
    return next(gid for gid, _, views in groups() if any(v == vid for v, _ in views))


def commands(app, app_tabs):
    names = dict(tabs())
    shown = [vid for vid, _ in tabs()]
    for vid in shown:
        yield SystemCommand(f"{names[vid]} tab ({GROUP_KEYS[_tab_of(vid)]})", f"go to tab · key {GROUP_KEYS[_tab_of(vid)]}",
                            lambda vid=vid: app.action_tab(vid), discover=False)
    current = app.active_tab
    for tid in [current, *(v for v in shown if v != current)]:
        found = rows(app.screen.active_bindings.items() if tid == current else tab_bindings(app, tid, app_tabs))
        for row in found:
            group, key, description, command, action, _ = row
            if action.partition("(")[0] in SKIPPED:
                continue
            yield SystemCommand(_title(description, key, action), f"on {names[tid]} · {command}",
                                lambda tid=tid, row=row: run(app, tid, row), discover=False)


def run(app, tid, row):
    group, _, description, _, action, node = row
    if tid != app.active_tab:
        app.action_tab(tid)

    def go():
        if group == "act on the selection" and not _has_selection(app, node):
            app.notify(f"{description}: pick a {'card' if tid == 'cards' else 'row'} first", markup=False)
        else:
            app.call_later(app.run_action, action, node)
    app.call_after_refresh(go)


def _has_selection(app, node):
    if callable(getattr(node, "_row", None)):
        return bool(node._row())
    focused = app.focused
    return focused is not None and not (isinstance(focused, DataTable) and focused.row_count == 0)
