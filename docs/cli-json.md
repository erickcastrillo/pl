# `--json` output

Every read command with `--json` prints exactly one JSON document on stdout and nothing else. The shape name below
is not part of the document; `src/pl/jsonout.py` (`SHAPES`) maps each command to it, and
`tests/snapshots/cli-json.json` pins the keys and value types. Secrets are masked.

## board
`pl list --json` (`--all` includes Done; `--product` leaves out `pipeline`)

```
{"pipeline": [{"id", "title", "column", "agent", "age_seconds"}], "intake": [same keys]}
```

`agent` is the short phrase `pl list` shows. `intake` is empty when no intake board is configured.

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
`pl usage [--since S] [--by card|account|model|loop] --json`

```
{"since", "by", "rows": [{"group", "input", "output", "cache_read", "cache_write", "total", "cost"}]}
```
`cost` is only there when `[usage] prices` is set. Accounts of other harnesses, which count no tokens, have no row.

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
