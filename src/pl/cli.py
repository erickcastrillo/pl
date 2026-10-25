"""pl - the feature funnel: idea -> spec -> plan -> your yes -> PR -> review -> merge-ready.

  pl idea "text" [--doc PATH]... [--title T] [--account NAME] [--repo NAME]... [--start] [--plan N --slice M]
                          drop an idea (and files) into Triage on the intake board, unassigned, to be
                          assessed; --start assigns it to you so the pipeline pulls it in;
                          --plan/--slice title the card 'Plan N slice M: <--title or first line>' and record both in its metadata
  pl list [--all]         both boards: the Feature Pipeline by column with agent state, then the intake board's
                          cards assigned to you (5 per column; --product shows them all; --all includes Done)
  pl inbox [--json]       every gate waiting on you, in every profile (--json: one document)
  pl review [id|slug]     open a plan in Neovim in a NEW iTerm window (--here: this terminal / a tmux window);
                          a card id pulls its plan into the plans folder first
  pl approve <id>         [--reason why] sync your local edits of the plan to the card, move it to Approved, notify
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
  pl depends <id> --on <id>... | --clear
                          record cards this card depends on, or clear them; the dispatcher starts an Approved card
                          only once every dependency is merged
  pl depends --from-plans [--dry-run]
                          record the Depends on line of every PLAN for cards with no dependencies yet
  pl board init           create the Inbox column if the board lacks it
  pl board migrate --to local|URL|SERVER [--yes] [--dry-run] [--skip-check] [--json]
                          copy every column and card to another board, check the copy, then point [tracker] at it
  pl card <id> [--delete|--json]
                          show a card's sections, metadata and stage caps, or delete it
  pl titles check [--json] [--fix [--dry-run]]
                          list plan cards whose 'Plan N slice M:' title and plan/slice metadata disagree, miss
                          one side, or share a pair with another card (Done left out); exits 1 when it finds
                          any (2: the board could not be read); --json prints them as a list; --fix adds the
                          missing prefix or metadata and leaves clashes for a person (exit 1 if any are left);
                          --dry-run says what it would change
  pl titles next          the next free plan number on the board (Done cards count)
  pl titles set <id> --plan N --slice M
                          title the card "Plan N slice M: ..." (any old prefix replaced) and record both in its metadata
  pl section <id> NAME [--from FILE [--force]]
                          print one section of a card in full; --from replaces SPEC, DESIGN or PLAN with the
                          file's text (- reads stdin). Stage agents write their result this way; an approved PLAN
                          or SPEC needs --force
  pl rework [--dry-run]    send cards in PR open whose pull request the review labeled for rework back to a run
                          agent now (the dispatcher does this every 10 min); after [dispatch] rework_rounds rounds
                          (3) the PR gets the failed label for a person
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
  pl caps set <id> [--minutes M] [--tokens 4M]
                          give one card its own time and token caps in place of the profile's; pl caps reset <id>
                          clears them; pl retry <id> restarts a card the dispatcher stopped at a cap
  pl usage [--since 24h|7d|YYYY-MM-DD] [--by card|account|model|loop] [--json] [--github]
                          tokens spent (input, output, cache read, cache write), read from Claude transcripts;
                          [usage] prices = {model = dollars per million tokens} adds a cost column; [usage]
                          windows = {model = context tokens} sets a context window (200k by default; a session
                          past 200k counts as 1M). A loop keeps its last 20 sessions: older ones show as "other".
                          Every Claude loop restarts fresh when idle above 80% of its window ([loops.<name>]
                          max_context = 60 sets another percent, 0 turns it off; one loop interval apart, at most
                          3 an hour; background shells or agents the session started end with it). --github
                          shows GitHub's GraphQL budget this hour instead: points left, the reset, what each
                          profile and caller spent and each profile's fair share
  pl hook <Notification|Stop> --card ID --window W
                          what a Claude Code stage agent's hooks run (pl adds them at launch; [dispatch] hooks = false
                          turns them off): a permission wait or a finished turn reaches the card at once. Exits 2
                          for a card or window that is not this profile's; any other failure exits 0
  pl alerts [--all] [--ack KEY]
                          open alerts (account parked, all accounts out, a stage failed 3 times, a dead loop,
                          GitHub rate limit, low memory, a runaway agent, a PR waiting over 24 h): each notifies
                          when it opens and again after 1 h and 4 h, and clears itself when its check passes;
                          --all adds the resolved ones; --ack stops the reminders until it clears
  pl intent <PR URL>      what a PR was meant to do: the spec + plan scope of the card behind it (for reviewers)
  pl sync status [--json]
                          how many changes are queued for the GitHub board and when the next window opens
  pl pause / pl resume    pause: the dispatcher starts no new agents; working agents finish their step, crashed
                          ones are still restarted and finished windows still closed. resume: back to normal
  pl kill-switch on|off|status [--json]
                          machine-wide kill switch: holds approved cards with outward actions; off releases them
  pl chat add|list|remove|test|route
                          messaging connectors (Telegram, Slack, Discord) for approvals and alerts from chat
  pl brain show CONV [--json] | prune [--dry-run] | end CONV | undo CONV
                          a brain conversation's turns; prune deletes those older than [brain] keep_days (180)
  pl ask "text" [--json] [--conversation ID]
                          ask the central read-only brain across every pipeline and connector
  pl manager start|stop [--all]|status|restart NAME
                          one manager per machine, on by default (every console starts it): it keeps every
                          profile's dispatcher running (those with [dispatch] autostart not false) and writes a
                          machine status the consoles read; machine.toml [manager] enabled = false turns it off
  pl worktrees list | gc [--dry-run] [--yes] [--json]
                          the worktrees pl-run made in the worktrees folder: their card, column and keep/remove;
                          gc lists what is safe to remove (finished card, clean, all pushed); --yes removes it
  pl update [--check]     print the commands that update pl (it never runs them); --check says whether a newer
                          release exists. The console and dispatcher check once a day (PL_NO_UPDATE_CHECK=1 or
                          [updates] check = false turns it off)
  pl local-model [check]  whether the optional local model ([local_model], Ollama on this machine) is on, its url
                          and model, and whether Ollama answers with that model pulled
  pl models list|test|set  list [--json] shows the models Ollama has pulled and the local job each is set for;
                          test JOB [--json] runs the job's sample 3 times (answer, schema pass|fail, ms);
                          set JOB MODEL [--yes] writes [models.local]; JOB is route, summarise, title or embed
  pl triggers add NAME --connector C --query Q --every 5m --pipeline P [--cap N] --yes [--dry-run]
                          turn matching incoming messages into cards; list [--json] shows status;
                          pause/resume NAME toggles whether it runs
  pl forget --contact <id|email> --yes [--dry-run] [--json]
                          remove one contact everywhere: cards, logs, memory; records an audit entry
  pl sandbox create|status|start|stop|destroy [--yes]
                          manage the Multipass sandbox VM for this profile (pl-sbx-NAME), mounting only its
                          work folder and worktrees folder; --yes destroys the VM without asking
  pl worktrees bootstrap PATH [--dry-run] [--card ID]
                          copy and link the files .pl/worktree.yaml lists from the main checkout into a new worktree
  pl e2e [REPO] [--ref REF] [--out DIR] [--keep-vm]
                          run the repo's Playwright suite in a throwaway Multipass VM set up from its
                          .pl/e2e.yaml; report in ~/pl-e2e/<repo>/<time>/ (pl e2e --help). No profile needed
  pl whatsnew             what each pl upgrade added, where to see it and a command to try (? in the console)
  pl pods rule add POD "TEXT" [--note N] [--yes] | rollback POD --to N [--yes] | history POD [--json]
                          pod.md is kept as numbered versions; a rule is added under "## Rules" as a new version,
                          and a rollback saves an earlier version's exact text as a new version
  pl skills list | share NAME [--account A] | link NAME ACCOUNT | reset NAME [--yes]
                          every account's skills, agents and commands; share moves a skill into the shared
                          library (~/.local/share/pl/skills, or [skills] library) and leaves a link; link adds a
                          library skill to another Claude or Codex account (Settings: Skills, or ctrl+p "Skills");
                          reset puts back the shipped version of a built-in stage skill (pl-spec, pl-plan, ...)
  pl connectors list [--json] | add NAME (COMMAND... | --url URL) [--pin VERSION] | approve-tools NAME | revoke NAME
                          the machine's MCP connectors (<machine dir>/connectors.toml): role, teams, access, tier,
                          status; a custom adapter is warned about; no token ever shows
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
  pl accounts [--reset NAME|all] [--check-trust]
                          which Claude profile is parked for running out of usage credits (the dispatcher
                          detects the limit on an agent's screen, parks that profile, and restarts the card
                          under the other one); --reset un-parks a profile by hand
                          --check-trust shows, per Claude account, whether the work folder and the worktrees folder
                          are trusted, with the command to fix each one that is not
  pl pods list [--json] | show NAME [--json]
                          the pipeline's pods: rules (pod.md), knowledge, skills, plugins, layer budgets and, with --json, 30-day quality numbers
  pl loops list [--json] | add NAME (--skill S | --prompt P) [--every E] [--hours H] [--account A] [--team T] [--writes] [--yes] [--dry-run]
          | pause NAME | resume NAME | rm NAME [--yes]
                          manage the profile's [loops.NAME] tables; --every is 30m, hourly, daily HH:MM, weekdays HH:MM or cron:<expr>
  pl assistant log "text" | idea save ... | idea file <idea-id>
                          the Assistant tab's live harness session uses these: log records a change it made outside pl; idea save drafts an idea brief, idea file
                          marks it ready (you file it in the console with ctrl+f, after a confirm)
  pl question <id> "text"
                          ask the owner a question; ends the agent's step and waits for an answer
  pl answer <id> "text"   answer a card's open question so its stage restarts

Settings come from a profile: pl --profile NAME, or PL_CONFIG_DIR. Boards are reached through the tracker
set in its config.toml ([tracker], [intake]). Card contract: sections open with a line `# PIPELINE: <NAME>`.
INPUT holds the idea and inlined documents, REVIEW NOTES holds your rejections. metadata.pipeline_mode
= "auto" marks funnel cards, metadata.profile picks the harness account, metadata.worker tracks the agent.
"""
import argparse
import os
import sys

from pl import alerts, assistant, config, events, hooks, links, loops, outward, pods, profiles, retention, rework, setup, skills, titles

from pl import config as C
from pl.commands import (cmd_adopt, cmd_approve, cmd_board, cmd_caps, cmd_card, cmd_section, cmd_done, cmd_drop, cmd_undrop, cmd_idea, cmd_intent, cmd_list, cmd_move,
                         cmd_outbox, cmd_pause, cmd_profiles as cmd_accounts, cmd_pull, cmd_reject, cmd_resume, cmd_retry,
                         cmd_review, cmd_restart, cmd_hold, cmd_unhold, cmd_depends, cmd_sync)
from pl.dispatch import cmd_dispatch
from pl.move_agent import cmd_move_agent
from pl.standup import cmd_standup
from pl.usage import cmd_usage
from pl.watch import cmd_watch
from pl.util import BadInput


PROFILE_HELP = "pl profile: settings, state and logs in ~/.pl-NAME (or $PL_CONFIG_DIR)"
# commands that change the pipeline; the event is a record for the Activity feed, not a guard
ASSISTANT_ACTIONS = ("approve", "reject", "done", "drop", "undrop", "move", "retry", "pause", "resume", "move-agent", "idea", "pull", "adopt",
                     "restart", "hold", "unhold", "depends")
NO_PROFILE = "pl: no profile yet: run pl setup to create one (or pick one with pl --profile NAME)"


class VersionAction(argparse.Action):
    def __call__(self, *_):
        from pl import update
        print(update.version_text())
        raise SystemExit(0)


def main():
    from pl.trackers.github import RateLimited
    try:
        return _main()
    except RateLimited as e:   # held, not failed: try again after the reset
        print(f"pl: {e.note}", file=sys.stderr)
        raise SystemExit(3) from None
    except SystemExit as e:
        if not isinstance(e.code, str):
            raise
        reason = "; ".join(l.strip() for l in e.code.splitlines() if l.strip())
        print(reason if reason.startswith("pl") else f"pl: {reason}", file=sys.stderr)
        raise SystemExit(2 if isinstance(e, BadInput) else 1) from None


def _main():
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
    if pre_a.rest[:1] == ["chat"]:             # machine-wide: no profile of its own
        from pl import chat
        return chat.cmd_chat(pre_a.rest[1:])
    if pre_a.rest[:1] == ["e2e"]:              # repo-driven: reads .pl/e2e.yaml, needs no tracker
        from pl import e2e
        return e2e.cmd_e2e(pre_a.rest[1:])
    if pre_a.rest[:1] == ["manager"]:          # machine-wide: no profile of its own
        from pl import manager
        return manager.cmd_manager(pre_a.rest[1:])
    if pre_a.rest[:1] == ["mcp"]:              # MCP server: brain or profile server
        from pl import mcp_serve
        return mcp_serve.cmd_mcp(pre_a.rest[1:], pre_a.profile)
    if pre_a.rest[:1] == ["ask"]:              # central brain: routed before profile load
        from pl import brain
        return brain.cmd_ask(pre_a.rest[1:])
    config.load(pre_a.profile)
    ap = argparse.ArgumentParser(prog="pl", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", metavar="NAME", help=PROFILE_HELP)
    ap.add_argument("--version", action=VersionAction, nargs=0, help="show program's version number and exit")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("idea"); p.add_argument("text"); p.add_argument("--doc", action="append", default=[])
    p.add_argument("--title"); p.add_argument("--account", choices=list(C.PROFILES)); p.add_argument("--repo", action="append", default=[])
    p.add_argument("--start", action="store_true", help="assign it to yourself so the pipeline pulls it in now")
    p.add_argument("--plan", type=int); p.add_argument("--slice", type=int)
    p = sub.add_parser("list"); p.add_argument("--json", action="store_true", help="print one JSON document (shape in docs/cli-json.md)"); p.add_argument("--all", action="store_true", help="include Done"); p.add_argument("--product", action="store_true", help="only the Product board, every card assigned to you")
    p = sub.add_parser("review"); p.add_argument("what", nargs="?"); p.add_argument("--here", action="store_true", help="open in this terminal (or a tmux window) instead of a new iTerm window")
    p = sub.add_parser("approve"); p.add_argument("id"); p.add_argument("--force", action="store_true"); p.add_argument("--confirm", action="store_true", help="approve a reply that goes to a recipient outside the thread")
    p.add_argument("--reason", help="why you approve; saved to memory with the card")
    p = sub.add_parser("reject"); p.add_argument("id"); p.add_argument("notes")
    p = sub.add_parser("outbox"); p.add_argument("action", choices=["cancel"]); p.add_argument("id")
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
    p = sub.add_parser("depends"); p.add_argument("id", nargs="?"); p.add_argument("--on", nargs="+", default=[], metavar="ID", help="cards this card depends on"); p.add_argument("--clear", action="store_true", help="remove every dependency"); p.add_argument("--from-plans", action="store_true", help="record the Depends on line of every PLAN for cards with no dependencies yet"); p.add_argument("--dry-run", action="store_true", help="with --from-plans: show what it would record")
    p = sub.add_parser("move"); p.add_argument("id"); p.add_argument("column"); p.add_argument("--pr", metavar="URL", help="also record this pull request on the card")
    p = sub.add_parser("link"); p.add_argument("id"); p.add_argument("target", metavar="PIPELINE:ID")
    p.add_argument("--relation", default="related"); p.add_argument("--one-side", action="store_true", help="write only this card, not the other")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("board")
    p.add_argument("action", choices=["init", "serve", "check", "migrate"])
    p.add_argument("server", nargs="?", help="server for check: local, url, or mcp-config server name")
    p.add_argument("--board", help="board id on the server")
    p.add_argument("--mcp-config", help="path to the harness's MCP config file")
    p.add_argument("--header-env", action="append", default=[], metavar="HEADER=VAR", help="set request header from environment variable")
    p.add_argument("--stdio", action="store_true")
    p.add_argument("--listen", metavar="HOST:PORT")
    p.add_argument("--token-env", metavar="VAR")
    p.add_argument("--json", action="store_true", help="output JSON")
    p.add_argument("--to", metavar="TARGET", help="migrate: local, a URL, or a server name in the MCP config")
    p.add_argument("--yes", action="store_true", help="migrate: do not ask before copying and switching")
    p.add_argument("--dry-run", action="store_true", help="migrate: print what would be copied and write nothing")
    p.add_argument("--skip-check", action="store_true", help="migrate: do not run pl board check on the target first (for a board you already checked)")
    p = sub.add_parser("card"); p.add_argument("--json", action="store_true", help="print one JSON document (shape in docs/cli-json.md)"); p.add_argument("id"); p.add_argument("--delete", action="store_true")
    p = sub.add_parser("section"); p.add_argument("--json", action="store_true", help="print one JSON document (shape in docs/cli-json.md)"); p.add_argument("id"); p.add_argument("name", help="SPEC, DESIGN, PLAN, INPUT or REVIEW NOTES")
    p.add_argument("--from", dest="from_", metavar="FILE", help="replace SPEC, DESIGN or PLAN with this file's text (- = stdin)")
    p.add_argument("--force", action="store_true", help="write a PLAN or SPEC a person already approved (only on their yes)")
    p = sub.add_parser("titles"); pt = p.add_subparsers(dest="titles_cmd", required=True)
    q = pt.add_parser("check"); q.add_argument("--json", action="store_true", help="print the flagged cards as a JSON list")
    q.add_argument("--fix", action="store_true", help="add the missing title prefix or plan/slice metadata; clashes are left for a person")
    q.add_argument("--dry-run", action="store_true", help="with --fix: say what would change, change nothing")
    pt.add_parser("next")
    q = pt.add_parser("set"); q.add_argument("id"); q.add_argument("--plan", type=int, required=True); q.add_argument("--slice", type=int, required=True)
    p = sub.add_parser("caps"); pc = p.add_subparsers(dest="caps_cmd", required=True)
    q = pc.add_parser("set"); q.add_argument("id"); q.add_argument("--minutes"); q.add_argument("--tokens", help="2M, 500k or a number")
    q = pc.add_parser("reset"); q.add_argument("id")
    p = sub.add_parser("retry"); p.add_argument("id", help="card id, #issue number, or all"); p.add_argument("--stage", choices=["spec", "design", "plan", "run"])
    p = sub.add_parser("restart"); p.add_argument("id"); p.add_argument("--stage", choices=["spec", "design", "plan", "run"])
    p = sub.add_parser("profiles"); p.add_argument("--json", action="store_true", help="print one JSON document (shape in docs/cli-json.md)"); ps = p.add_subparsers(dest="profiles_cmd")
    q = ps.add_parser("new"); q.add_argument("name"); q.add_argument("--from-current", action="store_true", help="copy the profile you are running as, not the defaults")
    q.add_argument("--from-legacy", action="store_true", help="copy the settings of the old one-file pl script")
    p = sub.add_parser("accounts"); p.add_argument("--reset", metavar="NAME|all", help="un-park a profile (or all) by hand"); p.add_argument("--json", action="store_true", help="print one JSON document (shape in docs/cli-json.md)")
    p.add_argument("--check-trust", action="store_true", help="show whether each Claude account trusts the work and worktrees folders (reads only)")
    p = sub.add_parser("watch"); p.add_argument("--interval", type=int, default=None); p.add_argument("--once", action="store_true", help="print one frame and exit")
    p.add_argument("--plain", action="store_true", help="refreshing text frame instead of the TUI")
    p.add_argument("--open", metavar="CARD", help="open this card on start")
    p.add_argument("--tab", metavar="TAB", help="activate this tab on start")
    sub.add_parser("pause"); sub.add_parser("resume")
    p = sub.add_parser("standup"); p.add_argument("--since", help="24h (default), 90m, 7d, YYYY-MM-DD or YYYY-MM-DDTHH:MM")
    p.add_argument("--markdown", action="store_true", help="Markdown for docs instead of plain text for chat")
    p.add_argument("--slack", action="store_true", help="Slack formatting (*bold*, • bullets, links) to paste in a message")
    p.add_argument("--summary", action="store_true", help="add a 2-3 sentence summary from the local model ([local_model])")
    p.add_argument("--json", action="store_true", help="print one JSON document (shape in docs/cli-json.md)")
    p = sub.add_parser("intent"); p.add_argument("pr", help="full PR URL"); p.add_argument("--json", action="store_true", help="print one JSON document (shape in docs/cli-json.md)")
    p = sub.add_parser("rework"); p.add_argument("--dry-run", action="store_true", help="say what would be sent back")
    p = sub.add_parser("retention"); ps = p.add_subparsers(dest="retention_cmd")
    q = ps.add_parser("run")
    q.add_argument("--dry-run", action="store_true", help="say what would be retained")
    q.add_argument("--json", action="store_true", help="print JSON")
    p.add_argument("--dry-run", action="store_true", help="say what would be retained")
    p.add_argument("--json", action="store_true", help="print JSON")
    p = sub.add_parser("usage"); p.add_argument("--since", help="24h (default), 90m, 7d, YYYY-MM-DD or YYYY-MM-DDTHH:MM")
    p.add_argument("--by", choices=["card", "account", "model", "loop"], default="account")
    p.add_argument("--json", action="store_true", help="print JSON: token usage with hook vs screen state transition counts, or with --by card minutes, tokens and caps per card and stage")
    p.add_argument("--github", action="store_true", help="GitHub's GraphQL budget this hour: left, reset, per profile and caller")
    p = sub.add_parser("hook"); p.add_argument("event", choices=hooks.EVENTS); p.add_argument("--card", required=True)
    p.add_argument("--window", required=True); p.add_argument("text", nargs="*", help="ignored: never run, never stored")
    p = sub.add_parser("alerts"); p.add_argument("--json", action="store_true", help="print one JSON document (shape in docs/cli-json.md)"); p.add_argument("--all", action="store_true", help="include resolved alerts")
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
    p = sub.add_parser("connectors"); pc = p.add_subparsers(dest="connectors_cmd", required=True)
    q = pc.add_parser("list"); q.add_argument("--json", action="store_true", help="machine-readable, without any secret")
    q = pc.add_parser("add"); q.add_argument("name"); q.add_argument("command", nargs="*"); q.add_argument("--url"); q.add_argument("--pin", help="the server version you expect; another version counts as a tool change")
    q.add_argument("--yes", action="store_true")
    q = pc.add_parser("revoke"); q.add_argument("name"); q.add_argument("--yes", action="store_true")
    q = pc.add_parser("approve-tools"); q.add_argument("name"); q.add_argument("--yes", action="store_true"); q.add_argument("--json", action="store_true")
    p = sub.add_parser("loops"); pl_ = p.add_subparsers(dest="loops_cmd", required=True)
    q = pl_.add_parser("list"); q.add_argument("--json", action="store_true")
    q = pl_.add_parser("add"); q.add_argument("name"); g = q.add_mutually_exclusive_group(required=True)
    g.add_argument("--skill"); g.add_argument("--prompt")
    q.add_argument("--every", help="30m, hourly, daily HH:MM, weekdays HH:MM or cron:<expr>"); q.add_argument("--hours", help="HH:MM-HH:MM [weekdays]")
    q.add_argument("--account"); q.add_argument("--team"); q.add_argument("--writes", action="store_true", help="let the loop write (default: read-only)")
    q.add_argument("--yes", action="store_true", help="do not ask first"); q.add_argument("--dry-run", action="store_true", help="say what would be added, add nothing")
    for verb in ("pause", "resume"):
        q = pl_.add_parser(verb); q.add_argument("name")
    q = pl_.add_parser("rm"); q.add_argument("name"); q.add_argument("--yes", action="store_true", help="do not ask first")
    q = pl_.add_parser("run"); q.add_argument("name"); q.add_argument("--wait", action="store_true", help="follow until the run ends and print its record"); q.add_argument("--json", action="store_true", help="with --wait: print the record as JSON")
    p = sub.add_parser("pods"); pp = p.add_subparsers(dest="pods_cmd", required=True)
    q = pp.add_parser("list"); q.add_argument("--json", action="store_true")
    q = pp.add_parser("show"); q.add_argument("name"); q.add_argument("--json", action="store_true")
    q = pp.add_parser("rule"); qa = q.add_subparsers(dest="rule_cmd", required=True)
    r = qa.add_parser("add"); r.add_argument("pod"); r.add_argument("text"); r.add_argument("--note"); r.add_argument("--yes", action="store_true", help="do not ask first")
    r = qa.add_parser("accept"); r.add_argument("id"); r.add_argument("--yes", action="store_true", help="do not ask first")
    r = qa.add_parser("dismiss"); r.add_argument("id")
    q = pp.add_parser("rollback"); q.add_argument("pod"); q.add_argument("--to", type=int, required=True); q.add_argument("--yes", action="store_true", help="do not ask first")
    q = pp.add_parser("history"); q.add_argument("pod"); q.add_argument("--json", action="store_true")
    q = pk.add_parser("enable"); q.add_argument("name", nargs="?", help="skill name"); q.add_argument("--plugin", help="plugin name")
    q.add_argument("--pod", required=True, help="pod name"); q.add_argument("--hooks", action="store_true", help="enable plugin hooks")
    q.add_argument("--yes", action="store_true", help="do not ask first")
    q = pk.add_parser("disable"); q.add_argument("name"); q.add_argument("--pod", required=True, help="pod name")
    q.add_argument("--yes", action="store_true", help="do not ask first")
    p = sub.add_parser("worktrees"); p.add_argument("action", choices=["list", "gc", "bootstrap"])
    p.add_argument("path", nargs="?", help="bootstrap: the new worktree")
    p.add_argument("--yes", action="store_true", help="gc: remove what the dry run lists (default: only list)")
    p.add_argument("--dry-run", action="store_true", help="gc: list only (the default); bootstrap: say what would be copied, linked and run")
    p.add_argument("--card", help="bootstrap: the card that gets the setup log and, on a failure, a note")
    p.add_argument("--json", action="store_true", help="gc: the list as JSON")
    p = sub.add_parser("update"); p.add_argument("--check", action="store_true", help="only say whether a newer pl exists")
    p = sub.add_parser("local-model"); p.add_argument("action", nargs="?", choices=["check"], default="check")
    p = sub.add_parser("models"); pm = p.add_subparsers(dest="models_cmd", required=True)
    q = pm.add_parser("list"); q.add_argument("--json", action="store_true", help="output as JSON")
    q = pm.add_parser("test"); q.add_argument("job"); q.add_argument("--json", action="store_true", help="output as JSON")
    q = pm.add_parser("set"); q.add_argument("job"); q.add_argument("model")
    q.add_argument("--yes", action="store_true", help="write without asking")
    p = sub.add_parser("inbox"); p.add_argument("--json", action="store_true", help="output JSON instead of plain text")
    p = sub.add_parser("kill-switch"); p.add_argument("action", choices=["on", "off", "status"])
    p.add_argument("--json", action="store_true", help="output as JSON")
    p = sub.add_parser("triggers"); pt = p.add_subparsers(dest="triggers_cmd")
    q = pt.add_parser("add")
    q.add_argument("name")
    q.add_argument("--connector", required=True)
    q.add_argument("--query", required=True)
    q.add_argument("--every", required=True)
    q.add_argument("--pipeline", required=True)
    q.add_argument("--cap", type=int, default=20)
    q.add_argument("--column")
    q.add_argument("--attach-to", action="append", default=None)
    q.add_argument("--yes", action="store_true")
    q.add_argument("--dry-run", action="store_true")
    q = pt.add_parser("list"); q.add_argument("--json", action="store_true")
    q = pt.add_parser("pause"); q.add_argument("name")
    q = pt.add_parser("resume"); q.add_argument("name")
    q = pt.add_parser("test"); q.add_argument("name"); q.add_argument("--replay", metavar="WINDOW", help="search this far back, like 7d"); q.add_argument("--json", action="store_true")
    p = sub.add_parser("question"); p.add_argument("id"); p.add_argument("text"); p.add_argument("--json", action="store_true")
    p = sub.add_parser("answer"); p.add_argument("id"); p.add_argument("text"); p.add_argument("--json", action="store_true")
    p = sub.add_parser("audit")
    p.add_argument("--since", help="7d, 24h, or date")
    p.add_argument("--json", action="store_true", help="output json lines")
    p.add_argument("--verify", action="store_true", help="verify the hash chain and exit 1 on broken chain")
    p = sub.add_parser("forget"); p.add_argument("--contact", required=True, help="contact email or id to forget")
    p.add_argument("--yes", action="store_true", help="confirm without prompting")
    p.add_argument("--dry-run", action="store_true", help="count what would be removed without changing anything")
    p.add_argument("--json", action="store_true", help="output counts as JSON")
    p = sub.add_parser("sandbox"); p.add_argument("action", choices=["create", "status", "start", "stop", "destroy"])
    p.add_argument("--yes", action="store_true", help="do not ask before destroying the VM")
    p = sub.add_parser("brain"); pb = p.add_subparsers(dest="brain_cmd")
    q = pb.add_parser("show"); q.add_argument("conv"); q.add_argument("--json", action="store_true")
    q = pb.add_parser("prune"); q.add_argument("--dry-run", action="store_true")
    q = pb.add_parser("end"); q.add_argument("conv")
    q = pb.add_parser("undo"); q.add_argument("conv")
    p = sub.add_parser("sync"); p.add_argument("action", choices=["status"]); p.add_argument("--json", action="store_true")
    a = ap.parse_args()
    is_board_check = a.cmd == "board" and getattr(a, "action", None) == "check"
    if C.CONFIG_DIR is None and a.cmd not in ("profiles", "update", "kill-switch", "brain") and not is_board_check and (a.cmd or (sys.stdin.isatty() and sys.stdout.isatty())):
        raise BadInput(NO_PROFILE)
    if not a.cmd:
        if sys.stdin.isatty() and sys.stdout.isatty():   # bare pl on a terminal opens the console; piped, it prints help
            return cmd_watch(argparse.Namespace(interval=None, once=False, plain=False, open=None, tab=None))
        ap.print_help()
        return
    from pl import ghquota
    ghquota.set_role(("assistant " if os.environ.get("PL_ASSISTANT") else "") + f"pl {a.cmd}")   # who spends GitHub's budget
    if os.environ.get("PL_ASSISTANT") and a.cmd in ASSISTANT_ACTIONS:   # the Activity feed shows the changes it made
        events.emit("assistant_action", getattr(a, "id", None) or getattr(a, "card", None), command=a.cmd)
    if os.environ.get("PL_SOURCE"):
        card_id = getattr(a, "id", None) or getattr(a, "card", None)
        events.emit("remote_action", card_id, command=a.cmd, source=os.environ["PL_SOURCE"])
        try:
            from pl import audit
            audit.record("remote_action", card=card_id, command=a.cmd, source=os.environ["PL_SOURCE"])
        except Exception:
            pass
    {"idea": cmd_idea, "list": cmd_list, "review": cmd_review, "approve": cmd_approve, "reject": cmd_reject,
     "outbox": cmd_outbox,
     "question": lambda a: __import__("pl.questions").questions.cmd_question(a),
     "answer": lambda a: __import__("pl.questions").questions.cmd_answer(a),
     "dispatch": cmd_dispatch, "board": cmd_board, "card": cmd_card, "caps": cmd_caps, "loops": loops.cmd_loops, "section": cmd_section, "pull": cmd_pull, "adopt": cmd_adopt, "done": cmd_done, "drop": cmd_drop, "undrop": cmd_undrop, "move": cmd_move, "retry": cmd_retry, "link": links.cmd_link,
     "restart": cmd_restart, "hold": cmd_hold, "unhold": cmd_unhold,
     "profiles": profiles.cmd_profiles, "accounts": cmd_accounts, "watch": cmd_watch, "pause": cmd_pause, "resume": cmd_resume, "intent": cmd_intent, "rework": rework.cmd_rework,
     "standup": cmd_standup, "usage": cmd_usage, "alerts": alerts.cmd_alerts, "move-agent": cmd_move_agent,
     "assistant": assistant.cmd_assistant, "skills": skills.cmd_skills, "pods": pods.cmd_pods,
     "triggers": lambda a: __import__("pl.triggers").triggers.cmd_triggers(a),
     "connectors": lambda a: __import__("pl.connectors").connectors.cmd_connectors(a),
     "forget": retention.cmd_forget,
     "sync": cmd_sync,
     "depends": cmd_depends,
     "titles": titles.cmd_titles,
     "hook": hooks.cmd_hook,
     "worktrees": lambda a: __import__("pl.worktrees").worktrees.cmd_worktrees(a),
     "brain": lambda a: __import__("pl.brain").brain.cmd_brain(a),
     "sandbox": lambda a: __import__("pl.sandbox").sandbox.cmd_sandbox(a),
     "retention": lambda a: __import__("pl.retention").retention.cmd_retention(a),
     "audit": lambda a: __import__("pl.audit").audit.cmd_audit(a),
     "update": lambda a: __import__("pl.update").update.cmd_update(a),
     "local-model": lambda a: __import__("pl.local_model").local_model.cmd_local_model(a),
     "models": lambda a: __import__("pl.local_jobs").local_jobs.cmd_models(a),
     "inbox": lambda a: __import__("pl.inbox").inbox.cmd_inbox(a),
     "kill-switch": outward.cmd_kill_switch}[a.cmd](a)
