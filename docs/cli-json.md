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

## merges
`pl merges --dry-run --json`

```
{"candidates": [{"card", "pr", "title", "rules": [{"rule", "ok", "why"}], "action"}]}
```
`ok` is `true`, `false`, or `null` for a rule not checked yet.
