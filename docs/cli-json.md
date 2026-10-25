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
{"id", "title", "column", "tags": [], "updated_at", "metadata": {}, "sections": {"NAME": chars}}
```

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
