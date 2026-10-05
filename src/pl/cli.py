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
  pl move <id> "<column>" [--pr URL]
                          move a pipeline card (a GitHub issue number works too) to a column; --pr records the
                          pull request in the card's pr_urls
  pl done <id> [--note]   move a pipeline card or a Product card to Done (your call); --note appends the evidence
  pl drop <id> [--reason TEXT]
                          a pipeline card no longer needed: stops its live agent, moves it to Done marked dropped
                          (not done) with who, when, why and the column it was in; its linked Product card goes to Done
  pl undrop <id>          put a dropped card back in the column it was dropped from (Inbox if unknown)
  pl hold <id> [--reason TEXT] / pl unhold <id>
                          hold a card (the parked tag, other tags kept): the dispatcher starts no agent on it; its
                          running agent is not stopped. unhold removes the tag and the reason
  pl board init           create the Inbox column if the board lacks it
  pl card <id> [--delete] show a card's sections and metadata, or delete it
  pl section <id> NAME [--from FILE [--force]]
                          print one section of a card in full; --from replaces SPEC, DESIGN or PLAN with the
                          file's text (- reads stdin). Stage agents write their result this way; an approved PLAN
                          or SPEC needs --force
  pl retry <id|#n|all> [--stage S]
                          start a card's failed agent fresh: clears its worker and attempt count (all: every
                          funnel card whose agent died too often; --stage: only an agent of that stage)
  pl restart <id> [--stage S]
                          stop a card's agent even while it runs (Ctrl-C in its window, as pl drop does) so the
                          dispatcher starts its stage fresh as attempt 1; --stage: only an agent of that stage
  pl watch [--interval SEC] [--plain] [--once]
                          TUI board: every funnel card with its column, agent, profile, tmux window and age; the
                          selected agent's live screen; keys to jump to it, review/approve/reject, open the card.
                          --plain prints a refreshing text frame instead; --once prints one frame and exits
  pl standup [--since 24h|7d|YYYY-MM-DD[THH:MM]] [--markdown|--slack] [--summary]
                          a short summary of the window to paste in chat: PRs merged/opened/closed, ideas,
                          specs, plans, cards done, what is in progress, what needs you, errors; --summary adds
                          2-3 plain sentences above it, written by the local model ([local_model])
  pl usage [--since 24h|7d|YYYY-MM-DD] [--by card|account|model|loop] [--github]
                          tokens spent (input, output, cache read, cache write), read from Claude transcripts;
                          [usage] prices = {model = dollars per million tokens} adds a cost column; [usage]
                          windows = {model = context tokens} sets a context window (200k by default; a session
                          past 200k counts as 1M). A loop keeps its last 20 sessions: older ones show as "other".
                          Every Claude loop restarts fresh when idle above 80% of its window ([loops.<name>]
                          max_context = 60 sets another percent, 0 turns it off; one loop interval apart, at most
                          3 an hour; background shells or agents the session started end with it). --github
                          shows GitHub's GraphQL budget this hour instead: points left, the reset, what each
                          profile and caller spent and each profile's fair share
  pl alerts [--all] [--ack KEY]
                          open alerts (account parked, all accounts out, a stage failed 3 times, a dead loop,
                          GitHub rate limit, low memory, a runaway agent, a PR waiting over 24 h): each notifies
                          when it opens and again after 1 h and 4 h, and clears itself when its check passes;
                          --all adds the resolved ones; --ack stops the reminders until it clears
  pl intent <PR URL>      what a PR was meant to do: the spec + plan scope of the card behind it (for reviewers)
  pl pause / pl resume    pause: the dispatcher starts no new agents; working agents finish their step, crashed
                          ones are still restarted and finished windows still closed. resume: back to normal
  pl manager start|stop [--all]|status|restart NAME
                          one manager per machine, on by default (every console starts it): it keeps every
                          profile's dispatcher running (those with [dispatch] autostart not false) and writes a
                          machine status the consoles read; machine.toml [manager] enabled = false turns it off
  pl update [--check]     print the commands that update pl (it never runs them); --check says whether a newer
                          release exists. The console and dispatcher check once a day (PL_NO_UPDATE_CHECK=1 or
                          [updates] check = false turns it off)
  pl local-model [check]  whether the optional local model ([local_model], Ollama on this machine) is on, its url
                          and model, and whether Ollama answers with that model pulled
  pl whatsnew             what each pl upgrade added, where to see it and a command to try (? in the console)
  pl skills list | share NAME [--account A] | link NAME ACCOUNT | reset NAME [--yes]
                          every account's skills, agents and commands; share moves a skill into the shared
                          library (~/.local/share/pl/skills, or [skills] library) and leaves a link; link adds a
                          library skill to another Claude or Codex account (Settings: Skills, or ctrl+p "Skills");
                          reset puts back the shipped version of a built-in stage skill (pl-spec, pl-plan, ...)
  pl profiles             every pl profile (~/.pl-NAME) and whether its dispatcher runs; warns when two share
                          a harness account, a tracker board or a tmux session
  pl setup [--yes ...]    create a profile by answering a few questions (or give every answer as a flag); its
                          stages run pl's built-in skills. --use-builtin-stages switches an existing profile to them
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
  pl assistant log "text" | idea save ... | idea file <idea-id>
                          the Assistant tab's live harness session uses these: log records a change it made outside pl; idea save drafts an idea brief, idea file
                          marks it ready (you file it in the console with ctrl+f, after a confirm)

Settings come from a profile: pl --profile NAME, or PL_CONFIG_DIR. Boards are reached through the tracker
set in its config.toml ([tracker], [intake]). Card contract: sections open with a line `# PIPELINE: <NAME>`.
INPUT holds the idea and inlined documents, REVIEW NOTES holds your rejections. metadata.pipeline_mode
= "auto" marks funnel cards, metadata.profile picks the harness account, metadata.worker tracks the agent.
"""
import argparse
import os
import sys

from pl import alerts, assistant, config, events, profiles, setup, skills

from pl import config as C
from pl.commands import (cmd_adopt, cmd_approve, cmd_board, cmd_card, cmd_section, cmd_done, cmd_drop, cmd_undrop, cmd_idea, cmd_intent, cmd_list, cmd_move,
                         cmd_pause, cmd_profiles as cmd_accounts, cmd_pull, cmd_reject, cmd_resume, cmd_retry,
                         cmd_review, cmd_restart, cmd_hold, cmd_unhold)
from pl.dispatch import cmd_dispatch
from pl.move_agent import cmd_move_agent
from pl.standup import cmd_standup
from pl.usage import cmd_usage
from pl.watch import cmd_watch


PROFILE_HELP = "pl profile: settings, state and logs in ~/.pl-NAME (or $PL_CONFIG_DIR)"
# commands that change the pipeline; the event is a record for the Activity feed, not a guard
ASSISTANT_ACTIONS = ("approve", "reject", "done", "drop", "undrop", "move", "retry", "pause", "resume", "move-agent", "idea", "pull", "adopt",
                     "restart", "hold", "unhold")
NO_PROFILE = "pl: no profile yet: run pl setup to create one (or pick one with pl --profile NAME)"


class VersionAction(argparse.Action):
    def __call__(self, *_):
        from pl import update
        print(update.version_text())
        raise SystemExit(0)


def main():
    # First pass: only a --profile given before the subcommand picks the pl profile.
    pre = argparse.ArgumentParser(prog="pl", add_help=False)
    pre.add_argument("--profile"); pre.add_argument("rest", nargs=argparse.REMAINDER)
    pre_a = pre.parse_known_args()[0]
    if pre_a.rest[:2] == ["profiles", "new"]:  # the new profile's folder does not exist yet, so nothing may load it
        return profiles.cmd_new(pre_a.rest[2:], pre_a.profile)
    if pre_a.rest[:1] == ["setup"]:            # likewise: setup creates the profile
        return setup.cmd_setup(pre_a.rest[1:], pre_a.profile)
    if pre_a.rest[:1] == ["whatsnew"]:         # the package's own list: no profile needed
        from pl import whatsnew
        return whatsnew.cmd_whatsnew(pre_a.rest[1:])
    if pre_a.rest[:1] == ["manager"]:          # machine-wide: no profile of its own
        from pl import manager
        return manager.cmd_manager(pre_a.rest[1:])
    config.load(pre_a.profile)
    ap = argparse.ArgumentParser(prog="pl", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", metavar="NAME", help=PROFILE_HELP)
    ap.add_argument("--version", action=VersionAction, nargs=0, help="show program's version number and exit")
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
    p = sub.add_parser("drop"); p.add_argument("id"); p.add_argument("--reason", help="why it is no longer needed (kept on the card)")
    p = sub.add_parser("undrop"); p.add_argument("id")
    p = sub.add_parser("hold"); p.add_argument("id"); p.add_argument("--reason", help="why it waits (kept on the card)")
    p = sub.add_parser("unhold"); p.add_argument("id")
    p = sub.add_parser("move"); p.add_argument("id"); p.add_argument("column"); p.add_argument("--pr", metavar="URL", help="also record this pull request on the card")
    p = sub.add_parser("board"); p.add_argument("action")
    p = sub.add_parser("card"); p.add_argument("id"); p.add_argument("--delete", action="store_true")
    p = sub.add_parser("section"); p.add_argument("id"); p.add_argument("name", help="SPEC, DESIGN, PLAN, INPUT or REVIEW NOTES")
    p.add_argument("--from", dest="from_", metavar="FILE", help="replace SPEC, DESIGN or PLAN with this file's text (- = stdin)")
    p.add_argument("--force", action="store_true", help="write a PLAN or SPEC a person already approved (only on their yes)")
    p = sub.add_parser("retry"); p.add_argument("id", help="card id, #issue number, or all"); p.add_argument("--stage", choices=["spec", "design", "plan", "run"])
    p = sub.add_parser("restart"); p.add_argument("id"); p.add_argument("--stage", choices=["spec", "design", "plan", "run"])
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
    p.add_argument("--summary", action="store_true", help="add a 2-3 sentence summary from the local model ([local_model])")
    p = sub.add_parser("intent"); p.add_argument("pr", help="full PR URL")
    p = sub.add_parser("usage"); p.add_argument("--since", help="24h (default), 90m, 7d, YYYY-MM-DD or YYYY-MM-DDTHH:MM")
    p.add_argument("--by", choices=["card", "account", "model", "loop"], default="account")
    p.add_argument("--github", action="store_true", help="GitHub's GraphQL budget this hour: left, reset, per profile and caller")
    p = sub.add_parser("alerts"); p.add_argument("--all", action="store_true", help="include resolved alerts")
    p.add_argument("--ack", metavar="KEY", help="acknowledge an open alert: no more reminders until it clears")
    p = sub.add_parser("move-agent"); p.add_argument("card"); p.add_argument("account", choices=list(C.PROFILES))
    p.add_argument("--yes", action="store_true", help="do not ask first")
    p = sub.add_parser("assistant"); pa = p.add_subparsers(dest="assistant_cmd")
    q = pa.add_parser("log"); q.add_argument("text", help="one line: what changed, which file or plugin")
    q = pa.add_parser("idea"); qi = q.add_subparsers(dest="idea_cmd", required=True)
    r = qi.add_parser("save"); r.add_argument("--id", help="update this draft (else a new one)"); r.add_argument("--title")
    r.add_argument("--brief", required=True, help="JSON: problem, who, outcome, in_scope, out_of_scope, repos, open_questions")
    r = qi.add_parser("file"); r.add_argument("id", help="the idea id pl assistant idea save printed")
    p = sub.add_parser("skills"); pk = p.add_subparsers(dest="skills_cmd")
    pk.add_parser("list"); q = pk.add_parser("share"); q.add_argument("name"); q.add_argument("--account", choices=list(C.PROFILES))
    q = pk.add_parser("link"); q.add_argument("name"); q.add_argument("account", nargs="?", default="all")
    q = pk.add_parser("reset"); q.add_argument("name"); q.add_argument("--yes", action="store_true", help="do not ask first")
    p = sub.add_parser("update"); p.add_argument("--check", action="store_true", help="only say whether a newer pl exists")
    p = sub.add_parser("local-model"); p.add_argument("action", nargs="?", choices=["check"], default="check")
    a = ap.parse_args()
    if C.CONFIG_DIR is None and a.cmd not in ("profiles", "update") and (a.cmd or (sys.stdin.isatty() and sys.stdout.isatty())):
        raise SystemExit(NO_PROFILE)
    if not a.cmd:
        if sys.stdin.isatty() and sys.stdout.isatty():   # bare pl on a terminal opens the console; piped, it prints help
            return cmd_watch(argparse.Namespace(interval=None, once=False, plain=False))
        ap.print_help()
        return
    from pl import ghquota
    ghquota.set_role(("assistant " if os.environ.get("PL_ASSISTANT") else "") + f"pl {a.cmd}")   # who spends GitHub's budget
    if os.environ.get("PL_ASSISTANT") and a.cmd in ASSISTANT_ACTIONS:   # the Activity feed shows the changes it made
        events.emit("assistant_action", getattr(a, "id", None) or getattr(a, "card", None), command=a.cmd)
    {"idea": cmd_idea, "list": cmd_list, "review": cmd_review, "approve": cmd_approve, "reject": cmd_reject,
     "dispatch": cmd_dispatch, "board": cmd_board, "card": cmd_card, "section": cmd_section, "pull": cmd_pull, "adopt": cmd_adopt, "done": cmd_done, "drop": cmd_drop, "undrop": cmd_undrop, "move": cmd_move, "retry": cmd_retry,
     "restart": cmd_restart, "hold": cmd_hold, "unhold": cmd_unhold,
     "profiles": profiles.cmd_profiles, "accounts": cmd_accounts, "watch": cmd_watch, "pause": cmd_pause, "resume": cmd_resume, "intent": cmd_intent,
     "standup": cmd_standup, "usage": cmd_usage, "alerts": alerts.cmd_alerts, "move-agent": cmd_move_agent,
     "assistant": assistant.cmd_assistant, "skills": skills.cmd_skills,
     "update": lambda a: __import__("pl.update").update.cmd_update(a),
     "local-model": lambda a: __import__("pl.local_model").local_model.cmd_local_model(a)}[a.cmd](a)
