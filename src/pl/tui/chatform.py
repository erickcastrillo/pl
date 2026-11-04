import os
import shlex
from rich.text import Text
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Checkbox, Input, OptionList, Static
from textual.widgets.option_list import Option

from pl import chat, util

ADAPTERS = ("telegram", "slack", "discord")
ROUTE_KINDS = ("gates", "alerts", "digest")
SETUP = {
    "telegram": "1. Create a bot with @BotFather and copy its token. 2. Put the token in the variable "
                "(export VAR=… in the shell that starts pl). 3. Send the bot a message. "
                "4. Find the chat id (getUpdates on the bot API) and enter it as the allowed id.",
    "slack": "1. Create a Slack app and add a bot token with chat:write. 2. Put the token in the variable. "
             "3. Invite the bot to the channel. 4. Enter the channel id as the allowed id.",
    "discord": "1. Create an application and a bot, and copy its token. 2. Put the token in the variable. "
               "3. Invite the bot to the server. 4. Enter the channel id (Developer Mode → Copy ID) as the allowed id.",
}


def _cli_equivalent(adapter, name, var, ids, routes):
    parts = ["pl", "chat", "add", adapter, "--token-env", var, "--allow", *ids, "--name", name, "--yes"]
    lines = [" ".join(shlex.quote(p) for p in parts)]
    held = {n: c.get("routes") or ["gates"] for n, c in chat.load().items()}
    for kind in ROUTE_KINDS:
        if kind == "gates" and "gates" in routes:
            continue
        if kind != "gates" and kind not in routes:
            continue
        names = [n for n, r in held.items() if kind in r]
        if names:
            lines.append(" ".join(["pl", "chat", "route", kind, *(shlex.quote(n) for n in names)]))
        else:
            lines.append(f"pl chat route {kind}: no CLI equivalent (it needs at least one name)")
    return " · ".join(lines)


def save(adapter, name, var, ids_text, routes, env=os.environ):
    """Write the connector through chat.add(); (ok, message). The token value is never read."""
    name, var = name.strip() or adapter, var.strip()
    token = "${" + var + "}"
    if not chat.VAR_RE.match(token):
        return False, chat.add(adapter, token, [], name)[1]
    ids = ids_text.replace(",", " ").split()
    if not ids:
        return False, "pl chat add: at least one --allow id is required"
    if var not in env:
        return False, f"{var} is not set: export {var}=… in the shell that starts pl, then restart pl"
    if not routes:
        return False, "pick at least one route"
    code, msg = chat.add(adapter, token, ids, name, routes=list(routes))
    if code != 0:
        return False, msg
    if "unchanged" in msg:
        return True, msg
    return True, f"{msg} · {_cli_equivalent(adapter, name, var, ids, routes)}"


class ChatAdapterPicker(ModalScreen):
    DEFAULT_CSS = """
    ChatAdapterPicker { align: center middle; }
    ChatAdapterPicker > Vertical { width: 60; height: auto; border: round $accent; padding: 1 2; background: $surface; }
    ChatAdapterPicker OptionList { height: auto; }
    """
    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def compose(self):
        with Vertical():
            yield Static(Text("New chat connector: which app?"))
            yield OptionList(*(Option(a.capitalize(), id=a) for a in ADAPTERS))
            yield Static(Text("enter pick · esc cancel", style="dim"))

    def on_mount(self):
        ol = self.query_one(OptionList)
        ol.highlighted = 0
        ol.focus()

    def on_option_list_option_selected(self, m):
        self.dismiss(m.option.id)

    def action_cancel(self):
        self.dismiss(None)


class ChatConnectorForm(ModalScreen):
    DEFAULT_CSS = """
    ChatConnectorForm { align: center middle; }
    ChatConnectorForm > Vertical { width: 90; height: auto; max-height: 90%; border: round $accent; padding: 1 2; background: $surface; }
    """
    BINDINGS = [
        Binding("ctrl+s", "save", "save"),
        Binding("escape", "cancel", "cancel"),
    ]

    def __init__(self, adapter):
        super().__init__()
        self.adapter = adapter

    def compose(self):
        with Vertical():
            yield Static(Text(f"New {self.adapter.capitalize()} connector", style="bold"))
            yield Static(Text(SETUP[self.adapter]), id="chat-setup")
            yield Input(value=self.adapter, id="chat-name")
            yield Input(placeholder="token variable name, e.g. TG_TOKEN", id="chat-var")
            yield Input(placeholder="allowed user or chat ids, separated by commas or spaces", id="chat-allow")
            for kind in ROUTE_KINDS:
                yield Checkbox(kind, kind == "gates", id=f"route-{kind}")
            yield Static("", id="chat-error")
            yield Static(Text("ctrl+s save · esc cancel", style="dim"))

    def on_input_submitted(self, m):
        self.action_save()

    def action_save(self):
        routes = [k for k in ROUTE_KINDS if self.query_one(f"#route-{k}", Checkbox).value]
        ok, msg = save(self.adapter, self.query_one("#chat-name", Input).value,
                       self.query_one("#chat-var", Input).value,
                       self.query_one("#chat-allow", Input).value, routes)
        if ok:
            self.dismiss(msg)
        else:
            self.query_one("#chat-error", Static).update(Text(util.mask(msg), style="red"))

    def action_cancel(self):
        self.dismiss(None)
