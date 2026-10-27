"""Console-to-command parity: maps console actions to pl CLI commands or marks them as navigation."""

COMMANDS = {
    # Plan 14 console actions mapped to pl CLI commands
    "PlApp.review": "pl approve <id>",
    "PlApp.dispatcher": "pl manager restart NAME",
    "PlApp.standup": "pl standup",
    "PlApp.whatsnew": "pl whatsnew",
    "PlApp.update": "pl update",
    "HomeView.select": "pl profiles",
    "HomeView.details": "pl profiles",
    "HomeView.get_started": "pl setup",
    "PlApp.switcher": "pl list --json",
    "PlApp.pipelines": "pl pipelines list --json",
    "PlApp.oldest": "pl inbox --json",
    "ConnectorsPanel.revoke": "pl connectors revoke NAME",
    "ConnectorsPanel.chat_add": "pl chat add ADAPTER --token-env VAR --allow ID --name NAME --yes",
    "ConnectorsPanel.chat_test": "pl chat test NAME",
    "PlApp.kill_switch": "pl kill-switch on|off|status",
    "CardScreen.approve": "pl approve <id>",
    "CardScreen.send_back": "pl reject <id> NOTES",
    "CardsView.drop": "pl drop <id>",
    "CardsView.undrop": "pl undrop <id>",
    "CardsView.hold": "pl hold <id>",
    "CardsView.adopt": "pl adopt <id>",
    "CardsView.done": "pl done <id>",
    "CardsView.move_column": "pl move <id> <col>",
    "CardsView.hand_off": "pl move <id> Manual",
    "CardsView.plan_filter": "pl list --plan N",
    "CardsView.retry": "pl retry <id>",
    "CardsView.restart": "pl restart <id>",
    "LoopsView.edit": "pl loops edit NAME",
    "LoopsView.pause": "pl loops pause|resume NAME",
    "LoopsView.run_now": "pl loops run NAME",
    "LoopsView.delete": "pl loops rm NAME",
    "CardsView.move_agent": "pl move-agent <card> <account>",
    "CardsView.edit_input": "pl section <id> INPUT",
    "AlertsView.ack": "pl alerts --ack KEY",
    "IdeasView.new": "pl idea",
    "IdeasView.approve": "pl approve <id>",
    "WaitingView.approve": "pl approve <id>",
    "WaitingView.send_back": "pl reject <id> NOTES",
    "ReviewScreen.approve": "pl approve <id>",
    "ReviewScreen.send_back": "pl reject <id> <notes>",
    "SkillsView.share": "pl skills share NAME",
    "SkillsView.link": "pl skills link NAME ACCOUNT",
    "AssistantView.undo": "pl brain undo CONV",
    "AssistantView.file_idea": "pl assistant idea file <id>",
    # Direct action aliases
    "drop": "pl drop <id>",
    "undrop": "pl undrop <id>",
    "hold": "pl hold <id>",
    "adopt": "pl adopt <id>",
    "done": "pl done <id>",
    "retry": "pl retry <id>",
    "standup": "pl standup",
    "dispatcher": "pl manager restart NAME",
    "whatsnew": "pl whatsnew",
    "update": "pl update",
    "kill_switch": "pl kill-switch on|off|status",
}

NAV = {
    "brain",
    "collapse_start",
    "dismiss_start",
    # Dialog and modal controls
    "close",
    "cancel",
    "back",
    "answer",
    "pick",
    # Tab and focus navigation
    "tab",
    "sub_tab",
    "sub_letter",
    "focus_next",
    "focus_previous",
    "app.focus_next",
    "app.focus_previous",
    "leave_box",
    # DataTable's own top and bottom keys, inherited by FitTable (the other table keys are listed below)
    "scroll_top",
    "scroll_bottom",
    # Window and screen navigation
    "cycle_window",
    "open_window",
    "jump",
    "live_view",
    "open_card",
    "open",
    "view_card",
    "step",
    # Refresh and lifecycle
    "refresh",
    "restart_console",
    "install",   # Skills panel: pl skills install is not in any merged slice yet
    "quit",
    "help",
    # Filters and display toggles
    "only_mine",
    "show_done",
    "next_filter",
    "mode",
    # In-place editing and local UI actions
    "edit",
    "external_edit",
    "save",
    "test",
    "rescan",
    "discard",
    "rename",
    "harness",
    "edit_brief",
    "skip",
    "new",
    "delete",
    "merge",
    "restart",
    # Clipboard and selection
    "copy",
    "screen.copy_text",
    "cut",
    "paste",
    "undo",
    "redo",
    "select_all",
    "select_line",
    "select_cursor",
    # Text input and cursor navigation
    "cursor_left",
    "cursor_right",
    "cursor_up",
    "cursor_down",
    "cursor_left_word",
    "cursor_right_word",
    "cursor_line_start",
    "cursor_line_end",
    "cursor_page_up",
    "cursor_page_down",
    "cursor_word_left",
    "cursor_word_right",
    "delete_left",
    "delete_right",
    "delete_left_word",
    "delete_right_word",
    "delete_left_all",
    "delete_right_all",
    "delete_line",
    "delete_word_left",
    "delete_word_right",
    "delete_to_start_of_line",
    "delete_to_end_of_line_or_delete_line",
    "home",
    "end",
    "submit",
    # Scrolling and standard widget keys
    "scroll_up",
    "scroll_down",
    "scroll_left",
    "scroll_right",
    "scroll_home",
    "scroll_end",
    "page_up",
    "page_down",
    "page_left",
    "page_right",
    "previous_tab",
    "next_tab",
    "press",
    "show_overlay",
}

LOOPS_KEYS = {"n": "pl loops add", "e": "pl loops edit", "p": "pl loops pause", "g": "pl loops run", "d": "pl loops rm"}   # w and R are navigation (NAV)

PARITY_MAP = {   # the pipeline details screen: what each of its actions runs
    "details": "pl pipelines show NAME --json",
    ("pipelines", "details"): "pl pipelines show NAME --json",
    ("details", "show"): "pl pipelines show NAME --json",
    "dispatcher": "pl manager restart NAME",
    ("details", "dispatcher"): "pl manager restart NAME",
    ("pods", "list"): "pl pods list",
    ("pods", "rule add"): "pl pods rule add",
    ("pods", "r"): "pl pods rule add",
    ("pods", "show"): "pl pods show",
    ("pods", "enter"): "pl pods show",
    ("triggers", "list"): "pl triggers list",
    ("models", "list"): "pl models list",
    ("models", "test"): "pl models test",
    ("models", "t"): "pl models test",
    ("models", "set"): "pl models set",
    ("models", "s"): "pl models set",
    ("chat", "list"): "pl chat list",
    ("chat", "add"): "pl chat add",
    ("chat", "a"): "pl chat add",
    ("chat", "test"): "pl chat test",
    ("chat", "t"): "pl chat test",
    ("chat", "route"): "pl chat route",
    ("chat", "o"): "pl chat route",
    ("skills", "list"): "pl skills list",
    ("skills", "install"): "pl skills install",
    ("skills", "I"): "pl skills install",
    ("skills", "update"): "pl skills update",
    ("skills", "u"): "pl skills update --check",
    ("connectors", "list"): "pl connectors list",
    ("connectors", "add"): "pl connectors add",
    ("connectors", "n"): "pl connectors add",
}
PARITY = {   # the card screen's own header actions (and the Connectors panel's)
    "card.approve": "pl approve", "card.send_back": "pl reject", "card.hold": "pl hold",
    "card.restart": "pl restart", "card.drop": "pl drop",
    ("card", "approve"): "pl approve", ("card", "send_back"): "pl reject",
    ("card", "hold"): "pl hold", ("card", "restart"): "pl restart", ("card", "drop"): "pl drop",
    "approve": "pl approve", "send_back": "pl reject", "hold": "pl hold", "restart": "pl restart", "drop": "pl drop",
    "connectors.add": "pl connectors add", "connectors.test": "pl connectors test", "connectors.revoke": "pl connectors revoke",
    ("connectors", "add"): "pl connectors add", ("connectors", "test"): "pl connectors test", ("connectors", "revoke"): "pl connectors revoke",
}
MAP = PARITY

NAVIGATE = {"tab", "step", "cycle_window", "focus_next", "focus_previous", "jump", "open_window", "switcher", "oldest",
            "close", "cancel", "back", "leave_box", "scroll_top", "scroll_bottom", "only_mine", "plan_filter",
            "next_filter", "show_done", "help"}
GROUPS = ("navigate", "act on the selection", "add", "other")


def _action_name(action):
    return action.split("(")[0].removeprefix("app.")


def command_for(owner, action, key=None):
    """The pl command a bound action runs, or None for navigation and local UI actions."""
    name = _action_name(action)
    for found in (LOOPS_KEYS.get(key) if owner == "LoopsView" else None,   # the key names the exact command (pause or resume)
                  COMMANDS.get(f"{owner}.{name}"), COMMANDS.get(name), PARITY.get(name)):
        if found:
            return found
    return None


def group_of(action, command):
    """Which help group a bound action belongs to (one of GROUPS)."""
    name = _action_name(action)
    if name in NAVIGATE:
        return "navigate"
    if name == "new" or name.startswith("new_") or name == "file_idea":
        return "add"
    if command and any(token in command for token in ("<id>", "<card>", "NAME")):
        return "act on the selection"
    return "other"
