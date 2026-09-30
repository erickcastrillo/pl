"""pl - the feature funnel: idea -> spec -> plan -> your yes -> PR -> review -> merge-ready.

  pl idea "text" [--doc PATH]... [--title T] [--account NAME] [--repo NAME]... [--start]
                          drop an idea (and files) into Triage on the intake board, unassigned, to be
                          assessed; --start assigns it to you so the pipeline pulls it in
  pl list [--all]         both boards: the Feature Pipeline by column with agent state, then the intake board's
                          cards assigned to you (5 per column; --product shows them all; --all includes Done)
  pl review [id|slug]     open a plan in Neovim in a NEW iTerm window (--here: this terminal / a tmux window);
                          a card id pulls its plan into the plans folder first
  pl approve <id>         sync your local edits of the plan to the card, move it to Approved, notify
  pl reject <id> "notes"  append review notes to the card and send it back to Spec ready for re-planning
  pl dispatch [--once] [--interval SEC] [--max-runs N] [--dry-run]
                          start the right agent for every waiting card, in the profile's tmux session
  pl pull [--list] [--dry-run] [<product-card-id>]
                          bridge from the intake board: cards newly assigned to you in Triage/Backlog
                          become funnel ideas (the dispatcher does this every pass); --list shows every
                          candidate; a card id pulls that one card whatever its column
  pl adopt <id>           flag an existing Feature Pipeline card for the funnel
  pl move <id> "<column>" move a pipeline card (a GitHub issue number works too) to a column
  pl done <id> [--note]   move a pipeline card or a Product card to Done (your call); --note appends the evidence
  pl board init           create the Inbox column if the board lacks it
  pl card <id> [--delete] show a card's sections and metadata, or delete it
  pl retry <id|#n|all> [--stage S]
                          start a card's failed agent fresh: clears its worker and attempt count (all: every
                          funnel card whose agent died too often; --stage: only an agent of that stage)
  pl watch [--interval SEC] [--plain] [--once]
                          TUI board: every funnel card with its column, agent, profile, tmux window and age; the
                          selected agent's live screen; keys to jump to it, review/approve/reject, open the card.
                          --plain prints a refreshing text frame instead; --once prints one frame and exits
  pl standup [--since 24h|7d|YYYY-MM-DD[THH:MM]] [--markdown|--slack]
                          a short summary of the window to paste in chat: PRs merged/opened/closed, ideas,
                          specs, plans, cards done, what is in progress, what needs you, errors
  pl usage [--since 24h|7d|YYYY-MM-DD] [--by card|account|model|loop]
                          tokens spent (input, output, cache read, cache write), read from Claude transcripts;
                          [usage] prices = {model = dollars per million tokens} adds a cost column; [usage]
                          windows = {model = context tokens} sets a context window (200k by default; a session
                          past 200k counts as 1M). A loop keeps its last 20 sessions: older ones show as "other".
                          [loops.<name>] max_context = 60 restarts that loop fresh when idle above 60% of its
                          window (off unless set; one loop interval apart, at most 3 an hour; background shells
                          or agents the session started end with it)
  pl alerts [--all] [--ack KEY]
                          open alerts (account parked, all accounts out, a stage failed 3 times, a dead loop,
                          GitHub rate limit, low memory, a runaway agent, a PR waiting over 24 h): each notifies
                          when it opens and again after 1 h and 4 h, and clears itself when its check passes;
                          --all adds the resolved ones; --ack stops the reminders until it clears
  pl intent <PR URL>      what a PR was meant to do: the spec + plan scope of the card behind it (for reviewers)
  pl pause / pl resume    pause: the dispatcher starts no new agents; working agents finish their step, crashed
                          ones are still restarted and finished windows still closed. resume: back to normal
  pl manager start|stop [--all]|status|restart NAME
                          one manager per machine: it keeps every profile's dispatcher running (those with
                          [dispatch] autostart not false) and writes a machine status the consoles read
  pl profiles             every pl profile (~/.pl-NAME) and whether its dispatcher runs; warns when two share
                          a harness account, a tracker board or a tmux session
  pl setup [--yes ...]    create a profile by answering a few questions (or give every answer as a flag)
  pl profiles new NAME [--from-current | --from-legacy]
                          create ~/.pl-NAME/config.toml (spec gate on) and print a shell alias for it;
                          --from-legacy copies the old one-file pl script's settings
  pl move-agent <card> <account> [--yes]
                          move a card's live Claude agent to another account in place: Ctrl-C in its window,
                          copy its transcript, resume the same session there (asks first unless --yes). The
                          dispatcher does this by itself when an agent hits its usage limit
  pl accounts [--reset NAME|all]
                          which Claude profile is parked for running out of usage credits (the dispatcher
                          detects the limit on an agent's screen, parks that profile, and restarts the card
                          under the other one); --reset un-parks a profile by hand

Settings come from a profile: pl --profile NAME, or PL_CONFIG_DIR. Boards are reached through the tracker
set in its config.toml ([tracker], [intake]). Card contract: sections open with a line `# PIPELINE: <NAME>`.
INPUT holds the idea and inlined documents, REVIEW NOTES holds your rejections. metadata.pipeline_mode
= "auto" marks funnel cards, metadata.profile picks the harness account, metadata.worker tracks the agent.
"""
import argparse
import sys

from pl import alerts, config, profiles, setup

from pl import config as C
from pl.commands import (cmd_adopt, cmd_approve, cmd_board, cmd_card, cmd_done, cmd_idea, cmd_intent, cmd_list, cmd_move,
                         cmd_pause, cmd_profiles as cmd_accounts, cmd_pull, cmd_reject, cmd_resume, cmd_retry,
                         cmd_review)
from pl.dispatch import cmd_dispatch
from pl.move_agent import cmd_move_agent
from pl.standup import cmd_standup
from pl.usage import cmd_usage
from pl.watch import cmd_watch


PROFILE_HELP = "pl profile: settings, state and logs in ~/.pl-NAME (or $PL_CONFIG_DIR)"
NO_PROFILE = "pl: no profile yet: run pl setup to create one (or pick one with pl --profile NAME)"


def main():
    # First pass: only a --profile given before the subcommand picks the pl profile.
    pre = argparse.ArgumentParser(prog="pl", add_help=False)
    pre.add_argument("--profile"); pre.add_argument("rest", nargs=argparse.REMAINDER)
    pre_a = pre.parse_known_args()[0]
    if pre_a.rest[:2] == ["profiles", "new"]:  # the new profile's folder does not exist yet, so nothing may load it
        return profiles.cmd_new(pre_a.rest[2:], pre_a.profile)
    if pre_a.rest[:1] == ["setup"]:            # likewise: setup creates the profile
        return setup.cmd_setup(pre_a.rest[1:])
    if pre_a.rest[:1] == ["manager"]:          # machine-wide: no profile of its own
        from pl import manager
        return manager.cmd_manager(pre_a.rest[1:])
    config.load(pre_a.profile)
    ap = argparse.ArgumentParser(prog="pl", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", metavar="NAME", help=PROFILE_HELP)
    ap.add_argument("--version", action="version", version=f"pl {__import__('pl').__version__}")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("idea"); p.add_argument("text"); p.add_argument("--doc", action="append", default=[])
    p.add_argument("--title"); p.add_argument("--account", choices=list(C.PROFILES)); p.add_argument("--repo", action="append", default=[])
    p.add_argument("--start", action="store_true", help="assign it to yourself so the pipeline pulls it in now")
    p = sub.add_parser("list"); p.add_argument("--all", action="store_true", help="include Done"); p.add_argument("--product", action="store_true", help="only the Product board, every card assigned to you")
    p = sub.add_parser("review"); p.add_argument("what", nargs="?"); p.add_argument("--here", action="store_true", help="open in this terminal (or a tmux window) instead of a new iTerm window")
    p = sub.add_parser("approve"); p.add_argument("id"); p.add_argument("--force", action="store_true")
    p = sub.add_parser("reject"); p.add_argument("id"); p.add_argument("notes")
    p = sub.add_parser("dispatch"); p.add_argument("--once", action="store_true"); p.add_argument("--interval", type=int, default=None)
    p.add_argument("--max-runs", type=int, default=None); p.add_argument("--dry-run", action="store_true")
    p.add_argument("--max-prep", type=int, default=None, help="max spec/design/plan agents in flight"); p.add_argument("--no-pull", action="store_true", help="do not pull from the Product board")
    p = sub.add_parser("pull"); p.add_argument("id", nargs="?"); p.add_argument("--list", action="store_true"); p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("adopt"); p.add_argument("id"); p.add_argument("--account", choices=list(C.PROFILES))
    p = sub.add_parser("done"); p.add_argument("id"); p.add_argument("--note", help="evidence appended to the card as '## Closed <date>'")
    p = sub.add_parser("move"); p.add_argument("id"); p.add_argument("column")
    p = sub.add_parser("board"); p.add_argument("action")
    p = sub.add_parser("card"); p.add_argument("id"); p.add_argument("--delete", action="store_true")
    p = sub.add_parser("retry"); p.add_argument("id", help="card id, #issue number, or all"); p.add_argument("--stage", choices=["spec", "design", "plan", "run"])
    p = sub.add_parser("profiles"); ps = p.add_subparsers(dest="profiles_cmd")
    q = ps.add_parser("new"); q.add_argument("name"); q.add_argument("--from-current", action="store_true", help="copy the profile you are running as, not the defaults")
    q.add_argument("--from-legacy", action="store_true", help="copy the settings of the old one-file pl script")
    p = sub.add_parser("accounts"); p.add_argument("--reset", metavar="NAME|all", help="un-park a profile (or all) by hand")
    p = sub.add_parser("watch"); p.add_argument("--interval", type=int, default=None); p.add_argument("--once", action="store_true", help="print one frame and exit")
    p.add_argument("--plain", action="store_true", help="refreshing text frame instead of the TUI")
    sub.add_parser("pause"); sub.add_parser("resume")
    p = sub.add_parser("standup"); p.add_argument("--since", help="24h (default), 90m, 7d, YYYY-MM-DD or YYYY-MM-DDTHH:MM")
    p.add_argument("--markdown", action="store_true", help="Markdown for docs instead of plain text for chat")
    p.add_argument("--slack", action="store_true", help="Slack formatting (*bold*, • bullets, links) to paste in a message")
    p = sub.add_parser("intent"); p.add_argument("pr", help="full PR URL")
    p = sub.add_parser("usage"); p.add_argument("--since", help="24h (default), 90m, 7d, YYYY-MM-DD or YYYY-MM-DDTHH:MM")
    p.add_argument("--by", choices=["card", "account", "model", "loop"], default="account")
    p = sub.add_parser("alerts"); p.add_argument("--all", action="store_true", help="include resolved alerts")
    p.add_argument("--ack", metavar="KEY", help="acknowledge an open alert: no more reminders until it clears")
    p = sub.add_parser("move-agent"); p.add_argument("card"); p.add_argument("account", choices=list(C.PROFILES))
    p.add_argument("--yes", action="store_true", help="do not ask first")
    a = ap.parse_args()
    if C.CONFIG_DIR is None and a.cmd != "profiles" and (a.cmd or (sys.stdin.isatty() and sys.stdout.isatty())):
        raise SystemExit(NO_PROFILE)
    if not a.cmd:
        if sys.stdin.isatty() and sys.stdout.isatty():   # bare pl on a terminal opens the console; piped, it prints help
            return cmd_watch(argparse.Namespace(interval=None, once=False, plain=False))
        ap.print_help()
        return
    {"idea": cmd_idea, "list": cmd_list, "review": cmd_review, "approve": cmd_approve, "reject": cmd_reject,
     "dispatch": cmd_dispatch, "board": cmd_board, "card": cmd_card, "pull": cmd_pull, "adopt": cmd_adopt, "done": cmd_done, "move": cmd_move, "retry": cmd_retry,
     "profiles": profiles.cmd_profiles, "accounts": cmd_accounts, "watch": cmd_watch, "pause": cmd_pause, "resume": cmd_resume, "intent": cmd_intent,
     "standup": cmd_standup, "usage": cmd_usage, "alerts": alerts.cmd_alerts, "move-agent": cmd_move_agent}[a.cmd](a)
