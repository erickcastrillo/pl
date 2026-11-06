"""The idea interview from the command line: pl idea --interview on a terminal, pl interview for scripts and agents.

Drafts and turns are the Ideas tab's own (ideas.py), so a draft moves freely between here and the console. What the
harness wrote and what people answered is data: it is cleaned of control characters before it reaches a terminal."""
import os
import re
import sys
import threading
import time

from pl import config as C
from pl import ideas
from pl.jsonout import emit
from pl.util import BadInput, ask, mask

CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")   # keeps tab and newline
SCRIPTABLE = 'pl interview start "text" --json, then pl interview answer and pl interview file'
NOT_WITH_INTERVIEW = (
    ("doc", "--doc", "a card filed from an interview holds only the brief; put what matters in your answers"),
    ("start", "--start", "the card goes to the pipeline's Inbox, where the dispatcher takes it up on its own"),
    ("plan", "--plan/--slice", "interview cards are not numbered into a plan; use plain pl idea for those"),
    ("slice", "--plan/--slice", "interview cards are not numbered into a plan; use plain pl idea for those"),
    ("dry_run", "--dry-run", "each question is a real harness run; pl interview file --dry-run checks a filing"),
)


def _safe(v):
    if isinstance(v, str):
        return CONTROL.sub("", v)
    if isinstance(v, list):
        return [_safe(x) for x in v]
    if isinstance(v, dict):
        return {k: _safe(x) for k, x in v.items()}
    return v


def _say(text, file=None):
    print(mask(_safe(str(text))), file=file or sys.stdout, flush=True)


def _doc(idea, **extra):
    """The one shape every step prints (docs/cli-json.md, interview)."""
    return _safe({"id": idea["id"], "title": idea.get("title"), "status": idea.get("status"), "question": idea.get("question"),
                  "clear": ideas.is_clear(idea), "brief": idea.get("brief") or ideas.empty_brief(),
                  "card_id": idea.get("card_id"), **extra})


def _load(idea_id):
    if not ideas.ID_RE.fullmatch(idea_id):
        raise BadInput(f"pl interview: {idea_id!r} is not an idea id; pl interview list shows them")
    if not (ideas._dir() / f"{idea_id}.json").exists():
        raise BadInput(f"pl interview: no idea {idea_id}; pl interview list shows them")
    return ideas.load(idea_id)


def _open(idea):
    """Only a draft still being interviewed takes another turn."""
    if idea.get("status") != "interviewing":
        card = f" (card {idea['card_id']})" if idea.get("card_id") else ""
        raise BadInput(f"pl interview: idea {idea['id']} is {idea.get('status')}{card}; it takes no more answers")
    return idea


def _new(text, title=None, account=None, repos=()):
    """A saved draft, before the first turn: an interrupted or failed first turn still leaves the text to go on from."""
    if not str(text).strip():
        raise BadInput("pl interview: the idea text is empty")
    if not C.PROFILES:
        raise BadInput("pl interview: no harness accounts are configured; add one to the profile first")
    for r in repos:
        if not ideas.TAG_RE.fullmatch(r):
            raise BadInput(f"pl interview: --repo {r!r} cannot be a card tag: use letters, digits, dot, dash or underscore")
    idea = ideas.new_idea(text, title=title, account=account)
    if repos:
        idea["pinned_repos"] = list(repos)
        idea["brief"]["repos"] = list(repos)
    return ideas.save(idea)


def _turn(idea, answer=None, chatty=None):
    """One harness turn, saved on success. (idea, error): on error the draft is as it was. Progress goes to stderr when a
    person can see it (a script's stderr keeps the one-line reason a failure prints)."""
    if sys.stderr.isatty() if chatty is None else chatty:
        _say(f"asking {idea.get('harness') or 'the harness'} (up to 180 s; each question spends account budget)...", sys.stderr)
    stop = threading.Event()

    def tick():
        start = time.monotonic()
        while not stop.wait(1):
            print(f"\r  waiting {int(time.monotonic() - start)} s ", end="", file=sys.stderr, flush=True)
    ticker = threading.Thread(target=tick, daemon=True) if sys.stderr.isatty() else None
    if ticker:
        ticker.start()
    try:
        new, err = ideas.turn(idea, answer)
    finally:
        stop.set()
        if ticker:
            ticker.join()
            print("\r\x1b[K", end="", file=sys.stderr, flush=True)
    if err:
        return idea, _safe(err)
    if new.get("ready_to_file"):
        new["ready_to_file"] = False       # that yes was to the old text
    return ideas.save(new), None


def _file(idea):
    done = ideas.approve(idea)
    if done.get("status") != "approved" or not done.get("card_id"):
        raise SystemExit(f"pl interview: idea {idea['id']} is being filed elsewhere; check with pl interview show {idea['id']}")
    return done


def _say_filed(idea):
    _say(f"created {idea['card_id']}  {idea.get('title')}\n  pipeline board, Inbox\n"
         "  the dispatcher writes its spec on its next pass")


# ---------- the interactive form ----------

def _need_terminal(form):
    if not (sys.stdin and sys.stdin.isatty()):
        raise BadInput(f"{form}: asks questions on a terminal and stdin is not one; use {SCRIPTABLE}")


def _pending(idea):
    """What to ask next: the question, or the first point left open when the harness asked none."""
    return idea.get("question") or next(iter((idea.get("brief") or {}).get("open_questions") or []), "")


def _read_answer():
    while True:
        text = ask("pl interview", "> ").strip()
        if text:
            return text


def _drive(idea):
    """Question by question until the brief is clear, then the brief, the title and a yes before anything is filed."""
    _say(f"idea {idea['id']}  {idea.get('title')}\n  Ctrl-C leaves the draft: pl interview resume {idea['id']} goes on")
    while not ideas.is_clear(idea):
        answer = None
        if idea.get("asked"):
            _say(f"\n{_pending(idea)}")
            answer = _read_answer()
        while True:
            new, err = _turn(idea, answer, chatty=True)
            if not err:
                break
            _say(f"pl interview: {err}", sys.stderr)
            ask("pl interview", "Nothing changed. Press enter to try again, or Ctrl-C to stop: ")
        idea = new
    _say(f"\nThe brief is clear.\nTitle: {idea.get('title')}\n\n{ideas.markdown(idea['brief'])}")
    if ask("pl interview", "File this as a card in the Inbox? Type yes to file: ").strip().lower() not in ("y", "yes"):
        _say(f"not filed. The draft stays: pl interview file {idea['id']} files it, or the Ideas tab in the console")
        return
    _say_filed(_file(idea))


def _interactive(idea):
    try:
        _drive(idea)
    except (KeyboardInterrupt, EOFError):
        _say(f"\ndraft {idea['id']} kept: pl interview resume {idea['id']} goes on, or the Ideas tab in the console", sys.stderr)
        raise SystemExit(130) from None


def cmd_idea_interview(a):
    """pl idea --interview "text": flags that cannot apply are refused, never ignored."""
    if os.environ.get("PL_LOOP_WRITES") == "0":
        raise BadInput("pl: read-only loop: file the action as an idea")
    for dest, flag, why in NOT_WITH_INTERVIEW:
        if getattr(a, dest, None):
            raise BadInput(f"pl idea --interview: {flag} does not apply to an interview: {why}")
    _need_terminal("pl idea --interview")
    _interactive(_new(a.text, a.title, a.account, a.repo))


# ---------- the scriptable steps ----------

def _print_state(idea):
    iid = idea["id"]
    _say(f"idea {iid}  {idea.get('title')}")
    if idea.get("status") != "interviewing":
        _say(f"{idea.get('status')}" + (f": card {idea['card_id']}" if idea.get("card_id") else ""))
    elif ideas.is_clear(idea):
        _say(f"\n{ideas.markdown(idea['brief'])}\nclear: pl interview file {iid} files it")
    elif not idea.get("asked"):
        _say(f"no question yet: pl interview answer {iid} asks the harness again")
    else:
        _say(f"question: {_pending(idea)}\nanswer with: pl interview answer {iid} \"your answer\"")


def _report(a, idea, err):
    """The step's one document or text, then a failed turn as a non-zero exit with the reason on stderr."""
    if getattr(a, "json", False):
        emit(_doc(idea, **({"error": err} if err else {})))
    elif not err:
        _print_state(idea)
    if err:
        again = f"pl interview answer {idea['id']}" + (' "your answer"' if idea.get("asked") else "")
        raise SystemExit(f"pl interview: {err}; draft {idea['id']} is unchanged, retry with {again}")


def _start(a):
    idea = _new(a.text, a.title, a.account, a.repo)
    _report(a, *_turn(idea))


def _answer(a):
    idea = _open(_load(a.idea_id))
    text = (a.text or "").strip()
    if not text and idea.get("asked"):
        raise BadInput(f"pl interview answer: idea {idea['id']} needs the answer as text")
    _report(a, *_turn(idea, text or None))


def _resume(a):
    _need_terminal("pl interview resume")
    _interactive(_open(_load(a.idea_id)))


def _show(a):
    idea = _load(a.idea_id)
    if a.json:
        return emit(_doc(idea, transcript=idea.get("transcript") or []))
    _print_state(idea)
    for e in idea.get("transcript") or []:
        _say(f"\n{e.get('role')}: {e.get('text')}")
    if not ideas.is_clear(idea):
        _say(f"\n{ideas.markdown(idea.get('brief') or ideas.empty_brief())}")


def _file_cmd(a):
    idea = _load(a.idea_id)
    if a.dry_run:
        if idea.get("status") == "discarded":
            raise SystemExit("pl: this idea was discarded")
        if idea.get("status") != "approved" and not ideas.is_clear(idea):
            raise SystemExit("pl: the brief is not clear yet: answer the open question first")
        return emit(_doc(idea, dry_run=True)) if a.json else _say(f"would file: {idea.get('title')}")
    done = _file(idea)
    return emit(_doc(done)) if a.json else _say_filed(done)


def _list(a):
    rows = [i for i in ideas.list_ideas() if a.all or i.get("status") not in ideas.CLOSED]
    if a.json:
        return emit({"ideas": [{k: v for k, v in _doc(i, created_at=i.get("created_at")).items() if k != "brief"} for i in rows]})
    if not rows:
        return _say("no open interviews")
    for i in rows:
        state = "clear" if ideas.is_clear(i) and i.get("status") == "interviewing" else i.get("status")
        _say(f"{i['id']}  {state:<13} {i.get('title')}")


def cmd_interview(a):
    try:
        {"start": _start, "answer": _answer, "resume": _resume, "show": _show, "file": _file_cmd, "list": _list}[a.interview_cmd](a)
    except KeyboardInterrupt:
        _say("\npl interview: interrupted; the last saved draft is kept", sys.stderr)
        raise SystemExit(130) from None
