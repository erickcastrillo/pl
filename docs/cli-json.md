# `--json` output

Every read command with `--json` prints exactly one JSON document on stdout and nothing else. The shape name below
is not part of the document; `src/pl/jsonout.py` (`SHAPES`) maps each command to it, and
`tests/snapshots/cli-json.json` pins the keys and value types. Secrets are masked.

## board
`pl list --json` (`--all` includes Done; `--product` leaves out `pipeline`)

```
{"pipeline": [{"id", "title", "column", "agent", "depends_on": [], "waiting_on": [], "age_seconds"}],
 "intake": [{"id", "title", "column", "agent", "age_seconds"}]}
```

`agent` is the short phrase `pl list` shows. `depends_on` lists the card ids a pipeline card waits for, and `waiting_on` the ones
not yet in Done (or dropped); both are empty when there are none. `intake` is empty when no intake board is configured.

## card
`pl card <id> --json`

```
{"id", "title", "column", "tags": [], "updated_at", "metadata": {}, "sections": {"NAME": chars},
 "caps": {"minutes", "tokens"}, "spent": {"minutes", "tokens"}}
```
`caps` is the stage's [caps.<stage>] (empty when none); `spent` is what the agent has used (tokens is null when the harness does not count them).

## section
`pl section <id> NAME --json` (read form). A card without that section exits 1 with the reason on stderr.

```
{"card", "name", "text"}
```

## alerts
`pl alerts [--all] --json`

```
{"alerts": [{"key", "severity", "title", "fix", "first_seen", "count", ...}]}
```

## usage
`pl usage [--since S] [--by account|model|loop] --json`

```
{"since", "by", "state_transitions": {"hook", "screen"},
 "usage": {group: {"input", "output", "cache_read", "cache_write", "cost", "unpriced"}}}
```
`--by card --json` prints minutes, tokens and caps per card and stage instead. `--json` does not combine with `--github`.

## standup
`pl standup [--since S] --json` (`--markdown`, `--slack` and `--summary` only change the text form)

```
{"since", "prs": {"merged"|"opened"|"closed": {"total", "items": [{"repo", "number", "title", "url", "yours"}]}},
 "pipeline": {"ideas added": n, ...}, "needs_you": ["title"], "errors": [{"message", "count"}]}
```
When GitHub cannot answer, `prs` is `{"unavailable": "reason"}`.

## accounts
`pl accounts [--reset NAME|all] --json`

```
{"accounts": [{"name", "parked", "until", "reason", "dir"}]}
```
`until` and `reason` are empty when the account is not parked.

## profiles
`pl profiles --json`

```
{"profiles": [{"name", "dir", "running", "holder", "tmux_session", "tracker", "board_id", "accounts": {name: dir}}],
 "warnings": ["text"]}
```

## intent
`pl intent <PR URL> --json`

```
{"pr", "cards": [{"id", "title", "spec", "plan_scope"}]}
```
`plan_scope` is the plan's scope sections (minimum change, acceptance, non-goals), joined; empty when there is no plan.

## merges
`pl merges [--dry-run] --json`

```
{"candidates": [{"card", "pr", "title", "rules": [{"rule", "ok", "why"}], "action", "loop"}]}
```
`ok` is `true`, `false`, or `null` for a rule not checked yet. `loop` is what the merge loop did (with `--dry-run`, would do) about
a failed rule: `"add needs-person and comment"`, `"already commented on this head"`, `"remove needs-person"`, or `null`.

## manager-status
`pl manager status --json`

```
{"running": bool, ...the manager's status.json: "at", "pid", "hold", "profiles": [{"name", "running", ...}]}
```
`{"running": false}` alone when no manager is running.

## local-model
`pl local-model --json` (exit 0 when `ok`, else 1)

```
{"enabled": bool, "url", "model", "ok": bool, "detail"}
```

## whatsnew
`pl whatsnew --json`

```
{"entries": [{"key", "title", "where", "try"}]}
```

## skills-list
`pl skills list --json`

```
{"skills": [{"name", "account", "kind", "origin", "in_library": bool, "linked": ["account"], "description", "source"?}], "library"}
```
`source` (where the skill was installed from) appears only for library skills installed with `pl skills install`.

## update-check
`pl update --check --json`

```
{"version", "enabled": bool, "latest": "vX.Y.Z" | null, "update_available": bool}
```
