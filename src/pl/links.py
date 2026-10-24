"""pl link: join cards across pipelines. Each side lists the other in metadata.links."""
import json
import subprocess
import sys

from pl import config as C
from pl import profiles
from pl.board import card, find_card, fresh_next, update


def _run(argv):
    """The one subprocess seam of this module; tests fake it."""
    return subprocess.run(argv, capture_output=True, text=True)


def _own_pipeline():
    return (getattr(C, "PIPELINE", None) or {}).get("name") or C.PROFILE_NAME


def _profile_of(pipeline):
    return next((r["name"] for r in profiles.list_profiles(check_lock=False) if r["name"] == pipeline), None)


def _edit(card_id, change):
    """Rewrite the card's whole links list from a fresh read; True when it changed."""
    fresh_next()
    old = list((card(card_id).get("metadata") or {}).get("links") or [])
    new = change(old)
    if new != old:
        update(card_id, metadata={"links": new})
    return new != old


def add(card_id, pipeline, other_id, relation="related", one_side=False):
    profile = None if one_side else _profile_of(pipeline)
    if profile is None and not one_side:
        print(f"pl link: no pipeline {pipeline!r} (a profile of that name)", file=sys.stderr)
        raise SystemExit(2)
    link = {"pipeline": pipeline, "id": other_id, "relation": relation}
    added = _edit(card_id, lambda links: links if link in links else [*links, link])
    if one_side:
        return
    r = _run([sys.executable, "-m", "pl", "--profile", profile, "link", other_id, f"{_own_pipeline()}:{card_id}",
              "--relation", relation, "--one-side"])
    if r.returncode != 0:
        if added:
            _edit(card_id, lambda links: [x for x in links if x != link])
        raise SystemExit(f"pl link: the {pipeline} side failed, nothing was linked: {(r.stderr or r.stdout or '').strip()[:200]}")


def cmd_link(a):
    pipeline, sep, other = a.target.partition(":")
    if not (sep and pipeline and other):
        raise SystemExit("pl link: the target is PIPELINE:ID")
    cid = find_card(a.id)["id"]
    add(cid, pipeline, other, a.relation, a.one_side)
    links = (card(cid).get("metadata") or {}).get("links") or []
    print(json.dumps(links, indent=1) if a.json else f"linked {cid} to {pipeline}:{other} ({a.relation})")
