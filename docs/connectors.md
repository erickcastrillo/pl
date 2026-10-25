# Connectors

A connector is an MCP server registered for the whole machine in `<machine dir>/connectors.toml`, as `[connectors.<name>]`.
pl code refers to roles, never to a product, so swapping the server behind a role is a config change.

## Config keys

- Where the server is, exactly one of: `url`, `command` (with `args`, `env`), or `mcp_config` + `server` (an entry in a harness's MCP file, read as the tracker reads it).
- `auth` (`oauth`, `token`, `none`), `token` (`"${VAR}"` or `"keychain"`, never a literal), `headers` (secret-looking ones must be `${VAR}`).
- `role`: a list of `payments`, `contacts`, `email`, `calendar`, `messaging`, `memory`. A connector with no role is a plain tool set for its teams.
- `teams`: pipeline names that may use it. `access`: `read` (default) or `read-write`. `tier`: `vendor`, `community` or `adapter`.
- `tools`: maps a standard role tool name to the server's own tool name. `read_tools` / `write_tools` override the read-only guess.

## Roles and standard tools

`connectors.role("payments")` returns the one enabled connector that fills the role for your team (the pipeline name, else the profile name); two matches is an error.
`connectors.call_role("payments", "payments_since", day="mon")` calls the standard tool through that connector's `tools` mapping.
A card keeps only `metadata.refs.<role>`, so replacing the server changes `[connectors.*]` and nothing on the card.

## Import from Claude Code

```
pl connectors import --from claude [--json]
pl connectors import --from claude --add NAME --team T --role R --yes
```

Lists the servers in each account's `.claude.json` and the work folder's `.mcp.json` (names and kinds only). `--add` writes `mcp_config` and `server`: no env, header or key value is copied, and `${VAR}` is expanded from your environment when the server starts. Credential files are never opened. `--from codex` exits 2 (its TOML MCP file is not supported yet).

## Tiers

`vendor` is the vendor's own server, `community` a third-party one, `adapter` one we or you wrote around an API; `pl connectors list` warns for adapters.

## Security

- The file holds no secret and listings never print one.
- A tool is a write tool unless the server marks it `readOnlyHint` or you list it in `read_tools`; tools that change without `pl connectors approve-tools` lose write access.
