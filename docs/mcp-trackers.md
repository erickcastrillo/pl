# Use any MCP board server as the tracker

pl can keep its cards on any board that has an MCP server (Model Context Protocol, the tool protocol harnesses use). You tell pl which of the server's tools does each card action. pl opens one session to the server per process and calls those tools.

## 1. What pl needs from the server

pl has eight card actions. Each one maps to one tool call.

| Action | When pl calls it | What the reply must hold |
| --- | --- | --- |
| `columns` | reading the board | a list of columns, each with a title and an id |
| `cards` | every dispatcher pass, `pl list`, the console | a list of cards |
| `card` | reading one card, and after every write to check it | one card (a list is fine: pl takes the first) |
| `create` | a new card (approved idea) | the new card, or nothing |
| `update` | writing title, description, tags, metadata, assignee or column | anything |
| `move` | optional: moving a card to another column | anything |
| `delete` | `pl card <id> --delete` | anything |
| `ensure_column` | `pl board init`, when Inbox is missing | the new column, with its id |

A card is an object with these keys. Missing keys get an empty default.

| Key | Meaning |
| --- | --- |
| `id` | the card id |
| `title`, `description` | text; the description holds the `# PIPELINE:` sections |
| `tags` | list of strings |
| `metadata` | an object the server stores and returns as-is; pl writes its own keys here |
| `list_id` | the id of the card's column |
| `updated_at` | ISO 8601 time with an offset |
| `assigned_to` | who the card is assigned to |

The reply must be JSON in the tool result's first text part. After a write, pl reads the card back and stops with an error if a field did not persist.

If `move` is not mapped, pl moves a card by sending `list_id` through `update`.

## 2. Point pl at the server your harness already has

The simplest setup reuses the harness's own MCP file, so no key is copied:

```toml
[tracker]
type = "mcp"
mcp_config = "~/project/.mcp.json"   # a JSON file with {"mcpServers": {...}}
server = "boards"                    # the server's name in that file
board_id = "YOUR-BOARD-ID"
```

- pl reads that server's entry each time it starts and uses it as-is. It never writes it to `config.toml`.
- A relative `mcp_config` path is read from the profile folder.
- `${VAR}` and `${VAR:-default}` in `command`, `args`, `env`, `url` and `headers` are filled from the environment, like Claude Code does. An unset `${VAR}` with no default stops pl with `set VAR`.
- Env and header values, args, and the URL's path and query are masked in pl's errors and logs.
- Only JSON MCP files work (Claude Code, Antigravity). Codex's TOML MCP config is not supported yet.

You can write the server inline instead:

```toml
[tracker]
type = "mcp"
board_id = "YOUR-BOARD-ID"
server = { command = "boards-mcp", args = ["--stdio"], env = { BOARDS_TOKEN = "${BOARDS_TOKEN}" } }
# or a remote server over streamable HTTP:
# server = { url = "https://boards.example.com/mcp", headers = { Authorization = "Bearer ${BOARDS_TOKEN}" } }
```

Inline values support `${VAR}` only, with no default. Put every secret in a `${VAR}`: only those values are masked.

Other keys: `timeout` (seconds per call, default 60) and `card_url` (a link template for the console, for example `"https://boards.example.com/b/{board_id}/c/{item_id}"`). Without `card_url` the console shows the card id.

`[intake]` takes the same keys, for a second board whose cards assigned to you become ideas.

## 3. The tools mapping

Each action gets a table `[tracker.tools.<action>]`:

| Key | Meaning |
| --- | --- |
| `tool` | the server's tool name |
| `args` | the tool's arguments; values may hold placeholders |
| `result` | dotted path to the useful part of the reply, for example `data.items` or `items.0` |
| `fields` | rename map, server key to pl key; applied to replies and reversed on writes |

Placeholders:

| Placeholder | Filled in for |
| --- | --- |
| `{board_id}` | every action, from `board_id` |
| `{item_id}` | `card`, `update`, `move`, `delete` |
| `{column}`, `{column_id}` | `create`, `move` (the column title and its id); `ensure_column` gets `{column}` |
| `{title}`, `{description}` | `create`, `ensure_column` |
| `{fields}` | `create`, `update`: the fields pl writes, as an object |

A value that is exactly one placeholder keeps its type, so `"{fields}"` passes an object. Inside a longer string the value is turned into text.

The `"*"` key spreads `{fields}` into the top-level arguments, for servers that take flat write arguments. If a spread field has the same name as a mapped argument, the mapped argument wins.

Extra keys per action:

- `columns`: `title_key` and `id_key` name the column's title and id keys (default `title` and `id`).
- `cards`: `page` names the server's page-number argument and `page_size` is its page length. pl then reads page 1, 2, and so on until a page is shorter than `page_size`, at most 100 pages. `query` maps pl's filter keys to server arguments; pl only uses `assigned_to`, and filters again itself.
- `ensure_column`: `id_key` names the new column's id key (default `id`).

### Worked example

A generic server named `boards` exposes one tool, `boards_api`, whose `action` argument picks a board, list or item action. Its lists are pl's columns and its items are cards.

```toml
[tracker]
type = "mcp"
mcp_config = "~/project/.mcp.json"
server = "boards"
board_id = "YOUR-BOARD-ID"

[tracker.tools.columns]
tool = "boards_api"
args = { action = "list_list", board_id = "{board_id}" }
result = "lists"

[tracker.tools.cards]
tool = "boards_api"
args = { action = "item_list", board_id = "{board_id}", per_page = 25 }
result = "items"
page = "page"
page_size = 25
query = { assigned_to = "assigned_to" }

[tracker.tools.card]
tool = "boards_api"
args = { action = "item_get", item_id = "{item_id}" }
result = "item"
fields = { name = "title", body = "description" }

[tracker.tools.create]
tool = "boards_api"
args = { action = "item_create", list_id = "{column_id}", "*" = "{fields}" }
result = "item"

[tracker.tools.update]
tool = "boards_api"
args = { action = "item_update", item_id = "{item_id}", "*" = "{fields}" }
result = "item"

[tracker.tools.move]
tool = "boards_api"
args = { action = "item_move", item_id = "{item_id}", target_list_id = "{column_id}" }

[tracker.tools.delete]
tool = "boards_api"
args = { action = "item_delete", item_id = "{item_id}" }

[tracker.tools.ensure_column]
tool = "boards_api"
args = { action = "list_create", board_id = "{board_id}", title = "{column}", description = "{description}" }
result = "list"
```

Here the `card` reply calls the title `name` and the description `body`. The `fields` map renames them for that action only. Give `cards`, `create` and `update` the same map if their replies and writes use those names too.

## 4. Find a server's tools and arguments

- Ask your harness. It already has the server: "list the tools of the boards MCP server and their arguments".
- Read the server's own docs.
- After writing the mapping, open the console, press `8` for Settings and `t` to test. pl lists the server's tools, names any mapped tool the server lacks, and counts the columns.
- `pl --profile NAME list` then shows whether cards and columns come back.

## 5. Board and columns

pl does not create boards. Create the board in the tool's own interface and set `board_id`.

pl needs the columns Inbox, Spec ready, Plan for review, Manual, Approved, In progress, PR open and Done. With `ensure_column` mapped, `pl board init` creates Inbox. Create the other columns in the tool's interface.

## 6. Importing an older setup

`pl profiles new NAME --from-legacy` reads the settings of an older one-file pl script (default `~/.local/bin/pl`, or `--legacy-script PATH`) without running it. It writes a new profile whose tracker and intake use `type = "mcp"`, point at the harness's `.mcp.json` (`--mcp-json`, default `<work dir>/.mcp.json`) and name the server (`--server`), and carry a full tools mapping for a boards-style server. pl copies no keys: only the file path and the server name.

## 7. Security

- Secrets stay in the harness's MCP file or in environment variables. pl never writes them to `config.toml`, logs or the screen.
- A secret written literally into an inline `server` table is not a `${VAR}`, so pl cannot tell it is a secret and does not mask it. That includes a token inside a URL path.
- A stdio server's error output goes to `mcp-<server>.log` in the profile's state folder, mode 0600, with known secrets masked.
- Card text is data. pl never runs it or puts it into a shell command unquoted.

## 8. Troubleshooting

| Message | Cause |
| --- | --- |
| `cannot read MCP server 'boards' from <path>` | The file is missing or not JSON. The error names only the path and server. |
| `<path> has no MCP server 'boards'` | Wrong `server` name. |
| `MCP tracker server did not start within 60s` | The command hangs or needs a login. Run it by hand or through the harness first. |
| `MCP tracker server failed to start: ...` | The command or URL is wrong. See `mcp-<server>.log` for a stdio server. |
| `the MCP tracker has no [tracker.tools.X] mapping` | Map action X. |
| `MCP tool T (X): non-JSON reply: ...` | The tool answered in prose. pl needs JSON; check the tool's arguments. Only the first 200 characters are shown. |
| `MCP tool T (X): reply has no 'path'` | Wrong `result` path. Call the tool through your harness and read the reply's shape. |
| `MCP tracker cards: expected a list` | `result` for `cards` points at an object, not the list. |
| `field X did not persist` | The server did not save a field, or saves it under another name: add a `fields` map. |

## Not supported yet

- Codex's TOML MCP config file.
- `${VAR:-default}` in an inline `server` table.
- Creating a board, or columns other than Inbox.
- pl suggesting a mapping from the server's tool list. You write it.
